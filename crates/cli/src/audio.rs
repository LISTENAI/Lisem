use anyhow::{Context, Result, bail, ensure};
use base64::Engine;
use lisem_core::manager::Manager;
use serde_json::{Value, json};
use std::{
    fs::{File, OpenOptions},
    io::{Seek, SeekFrom, Write},
    path::Path,
    sync::atomic::{AtomicBool, Ordering},
    time::{Duration, Instant},
};

fn current_run(manager: &mut Manager, id: &str) -> Result<Value> {
    let value = manager.call("inspect", json!({"id":id}))?;
    let session = &value["runtime"]["session"];
    ensure!(
        session["finished"] == false && session["output"].is_string(),
        "Instance is not running"
    );
    Ok(session["output"].clone())
}

pub fn input(
    manager: &mut Manager,
    id: &str,
    path: &Path,
    timeout: Option<u64>,
    stop: &AtomicBool,
) -> Result<Value> {
    let run = current_run(manager, id)?;
    let mut progress = manager.call("audio", json!({"id":id,"run":run,"path":path}))?;
    let sequence = progress["sequence"]
        .as_u64()
        .context("Runtime does not expose injection progress; update the runtime")?;
    let timeout = timeout.unwrap_or_else(|| progress["samples"].as_u64().unwrap_or(0).div_ceil(16000) + 90);
    let deadline = Instant::now() + Duration::from_secs(timeout);
    loop {
        ensure!(
            progress["sequence"] == sequence,
            "Audio injection was replaced before completion was observed"
        );
        match progress["status"].as_str() {
            Some("completed" | "cancelled") => return Ok(progress),
            Some("running") => {}
            Some("failed") => bail!("Audio injection failed: {}", progress["error"]),
            Some("interrupted") => bail!("Run ended before audio injection completed"),
            _ => bail!("Audio injection outcome is unknown"),
        }
        if stop.load(Ordering::Acquire) || Instant::now() >= deadline {
            let result = manager.call(
                "audio_cancel",
                json!({"id":id,"run":run,"sequence":sequence}),
            )?;
            ensure!(
                stop.load(Ordering::Acquire),
                "Audio injection deadline reached; remaining input cancelled"
            );
            return Ok(result);
        }
        std::thread::sleep(Duration::from_millis(20));
        let value = manager.call("inspect", json!({"id":id}))?;
        let session = &value["runtime"]["session"];
        ensure!(
            session["output"] == run,
            "Run changed before injection completed"
        );
        ensure!(
            session["finished"] == false && session["error"].is_null(),
            "Device stopped before injection completed: {}",
            session["error"]
        );
        progress = session["audio_input"].clone();
    }
}

struct Wave {
    file: File,
    rate: u32,
    samples: u64,
    origin: u64,
    last_ns: Option<u64>,
}
impl Wave {
    fn create(path: &Path) -> Result<Self> {
        let file = OpenOptions::new().create_new(true).write(true).open(path)?;
        let mut result = Self {
            file,
            rate: 0,
            samples: 0,
            origin: 0,
            last_ns: None,
        };
        result.header()?;
        Ok(result)
    }
    fn header(&mut self) -> Result<()> {
        let size = u32::try_from(self.samples * 2).context("WAV size limit reached")?;
        let rate = if self.rate == 0 { 16000 } else { self.rate }; // Empty recordings use the board's normal rate.
        let mut header = Vec::with_capacity(44);
        header.extend(b"RIFF");
        header.extend(
            size.checked_add(36)
                .context("WAV size limit reached")?
                .to_le_bytes(),
        );
        header.extend(b"WAVEfmt ");
        header.extend(16u32.to_le_bytes());
        header.extend(1u16.to_le_bytes());
        header.extend(1u16.to_le_bytes());
        header.extend(rate.to_le_bytes());
        header.extend((rate * 2).to_le_bytes());
        header.extend(2u16.to_le_bytes());
        header.extend(16u16.to_le_bytes());
        header.extend(b"data");
        header.extend(size.to_le_bytes());
        self.file.seek(SeekFrom::Start(0))?;
        self.file.write_all(&header)?;
        self.file.seek(SeekFrom::End(0))?;
        self.file.flush()?;
        Ok(())
    }
    fn silence_to(&mut self, target: u64) -> Result<()> {
        ensure!(
            target <= (u32::MAX as u64 - 36) / 2,
            "WAV size limit reached"
        );
        let zero = [0u8; 8192];
        while self.samples < target {
            let count = (target - self.samples).min(4096) as usize;
            self.file.write_all(&zero[..count * 2])?;
            self.samples += count as u64;
        }
        Ok(())
    }
    fn append(&mut self, bytes: &[u8]) -> Result<()> {
        ensure!(
            bytes.len().is_multiple_of(16),
            "Invalid audio recording frames"
        );
        for frame in bytes.chunks_exact(16) {
            let ns = u64::from_le_bytes(frame[..8].try_into().unwrap());
            let rate = u32::from_le_bytes(frame[8..12].try_into().unwrap());
            ensure!(
                [8000, 16000, 24000, 32000, 48000, 96000].contains(&rate),
                "Invalid DAC sample rate"
            );
            ensure!(
                self.rate == 0 || self.rate == rate,
                "DAC sample rate changed; WAV recording stopped"
            );
            ensure!(
                ns >= self.origin && self.last_ns.is_none_or(|previous| ns > previous),
                "Audio sample timestamp went backwards"
            );
            self.rate = rate;
            let target = (((ns - self.origin) as u128 * rate as u128 + 500_000_000) / 1_000_000_000)
                .saturating_sub(1) as u64;
            self.silence_to(target)?;
            ensure!(
                self.samples < (u32::MAX as u64 - 36) / 2,
                "WAV size limit reached"
            );
            self.file.write_all(&frame[12..14])?;
            self.samples += 1;
            self.last_ns = Some(ns);
        }
        self.header()
    }
}
impl Drop for Wave {
    fn drop(&mut self) {
        let _ = self.header();
    }
}

pub fn output(manager: &mut Manager, id: &str, path: &Path, stop: &AtomicBool) -> Result<Value> {
    let run = current_run(manager, id)?;
    let mut wave = Wave::create(path)?;
    let mut packet = manager.call("audio_record", json!({"id":id,"run":run,"action":"start"}))?;
    let sequence = packet["sequence"]
        .as_u64()
        .context("Missing recording sequence")?;
    wave.origin = packet["started_ns"]
        .as_u64()
        .context("Missing recording start time")?;
    let mut offset = 0;
    let mut stopping = false;
    let result = (|| -> Result<Value> {
        loop {
            ensure!(
                packet["sequence"] == sequence && packet["offset"] == offset,
                "Audio recording cursor lost"
            );
            let bytes = base64::engine::general_purpose::STANDARD
                .decode(packet["data"].as_str().context("Missing audio data")?)?;
            wave.append(&bytes)?;
            offset += (bytes.len() / 16) as u64;
            ensure!(packet["error"] == "", "{}", packet["error"]);
            if stopping && packet["remaining"] == 0 {
                let end = packet["virtual_ns"]
                    .as_u64()
                    .context("Missing recording end time")?;
                let rate = if wave.rate == 0 { 16000 } else { wave.rate };
                wave.silence_to(
                    ((end - wave.origin) as u128 * rate as u128 / 1_000_000_000) as u64,
                )?;
                wave.header()?;
                return Ok(
                    json!({"status":"completed","path":path,"samples":wave.samples,"rate":rate}),
                );
            }
            ensure!(
                stopping || packet["active"] == true,
                "Recording ended unexpectedly"
            );
            let action = if !stopping && stop.load(Ordering::Acquire) {
                stopping = true;
                "stop"
            } else {
                "read"
            };
            if !stopping && packet["remaining"] == 0 {
                std::thread::sleep(Duration::from_millis(50));
            }
            packet = manager.call(
                "audio_record",
                json!({"id":id,"run":run,"action":action,"sequence":sequence}),
            )?;
        }
    })();
    if result.is_err() {
        // Release this observer only. Never stop firmware or another recording.
        let _ = manager.call(
            "audio_record",
            json!({"id":id,"run":run,"action":"discard","sequence":sequence}),
        );
    }
    result
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn wav_preserves_raw_samples_rate_and_virtual_silence() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("capture.wav");
        let mut wave = Wave::create(&path).unwrap();
        assert!(Wave::create(&path).is_err());
        let mut frames = Vec::new();
        for (ns, sample) in [(125000u64, 111i16), (500000, -222)] {
            frames.extend(ns.to_le_bytes());
            frames.extend(8000u32.to_le_bytes());
            frames.extend(sample.to_le_bytes());
            frames.extend([0, 0]);
        }
        wave.append(&frames).unwrap();
        let bytes = std::fs::read(&path).unwrap();
        assert_eq!(u32::from_le_bytes(bytes[24..28].try_into().unwrap()), 8000);
        assert_eq!(u32::from_le_bytes(bytes[40..44].try_into().unwrap()), 8);
        assert_eq!(&bytes[44..], &[111, 0, 0, 0, 0, 0, 34, 255]);
        assert!(wave.append(&frames).is_err()); // Old timestamps cannot silently concatenate.
        let mut changed = frames[..16].to_vec();
        changed[..8].copy_from_slice(&625000u64.to_le_bytes());
        changed[8..12].copy_from_slice(&16000u32.to_le_bytes());
        assert!(wave.append(&changed).is_err());
        assert_eq!(wave.samples, 4);
    }
}
