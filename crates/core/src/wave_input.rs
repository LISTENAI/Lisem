//! Seek over RIFF chunks without loading sample data into memory.
use anyhow::{Context, Result, ensure};
use std::{
    fs::File,
    io::{Read, Seek, SeekFrom},
    path::Path,
};

pub fn samples(path: &Path) -> Result<(u64, u64)> {
    let mut file = File::open(path)?;
    let mut header = [0; 12];
    file.read_exact(&mut header)?;
    ensure!(
        &header[..4] == b"RIFF" && &header[8..] == b"WAVE",
        "Invalid WAV input"
    );
    let end = u32::from_le_bytes(header[4..8].try_into()?) as u64 + 8;
    ensure!(
        end >= 12 && end <= file.metadata()?.len(),
        "Truncated WAV input"
    );
    let mut at = 12;
    let mut format = false;
    let mut samples = None;
    while at + 8 <= end {
        file.seek(SeekFrom::Start(at))?;
        let mut chunk = [0; 8];
        file.read_exact(&mut chunk)?;
        let length = u32::from_le_bytes(chunk[4..].try_into()?) as u64;
        ensure!(at + 8 + length <= end, "Truncated WAV chunk");
        match &chunk[..4] {
            b"fmt " => {
                ensure!(!format && length >= 16, "Invalid WAV format");
                let mut value = [0; 16];
                file.read_exact(&mut value)?;
                ensure!(
                    u16::from_le_bytes(value[0..2].try_into()?) == 1
                        && u16::from_le_bytes(value[2..4].try_into()?) == 1
                        && u32::from_le_bytes(value[4..8].try_into()?) == 16000
                        && u32::from_le_bytes(value[8..12].try_into()?) == 32000
                        && u16::from_le_bytes(value[12..14].try_into()?) == 2
                        && u16::from_le_bytes(value[14..16].try_into()?) == 16,
                    "Input must be mono PCM16 WAV at 16000 Hz"
                );
                format = true;
            }
            b"data" => {
                ensure!(
                    samples.is_none()
                        && length > 0
                        && length % 2 == 0
                        && length <= 16000 * 2 * 86400,
                    "Invalid WAV sample count"
                );
                samples = Some((at + 8, length));
            }
            _ => {}
        }
        at += 8 + length + length % 2;
    }
    ensure!(format, "Missing WAV format");
    samples.context("Missing WAV samples")
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;
    #[test]
    fn rejects_invalid_format_and_preserves_sample_range() {
        let mut bytes = b"RIFF".to_vec();
        bytes.extend(44u32.to_le_bytes());
        bytes.extend(b"WAVEfmt ");
        bytes.extend(16u32.to_le_bytes());
        bytes.extend([1, 0, 1, 0]);
        bytes.extend(16000u32.to_le_bytes());
        bytes.extend(32000u32.to_le_bytes());
        bytes.extend([2, 0, 16, 0]);
        bytes.extend(b"data");
        bytes.extend(8u32.to_le_bytes());
        bytes.extend([0, 128, 255, 127, 1, 0, 255, 255]);
        let file = tempfile::NamedTempFile::new().unwrap();
        std::fs::write(file.path(), &bytes).unwrap();
        assert_eq!(samples(file.path()).unwrap(), (44, 8));
        let original = bytes.clone();
        for (offset, value) in [
            (20, 3),
            (22, 2),
            (24, 0),
            (29, 0),
            (32, 4),
            (34, 8),
            (40, 7),
        ] {
            bytes.clone_from(&original);
            bytes[offset] = value;
            std::fs::write(file.path(), &bytes).unwrap();
            assert!(samples(file.path()).is_err());
        }
    }
    #[test]
    fn seeks_large_samples_and_rejects_truncation() {
        let mut file = tempfile::NamedTempFile::new().unwrap();
        let length = 32000u32 * 28800;
        file.write_all(b"RIFF").unwrap();
        file.write_all(&(length + 36).to_le_bytes()).unwrap();
        file.write_all(b"WAVEfmt \x10\0\0\0\x01\0\x01\0").unwrap();
        file.write_all(&16000u32.to_le_bytes()).unwrap();
        file.write_all(&32000u32.to_le_bytes()).unwrap();
        file.write_all(b"\x02\0\x10\0data").unwrap();
        file.write_all(&length.to_le_bytes()).unwrap();
        file.as_file().set_len(length as u64 + 44).unwrap();
        assert_eq!(samples(file.path()).unwrap(), (44, length as u64));
        file.as_file().set_len(length as u64 + 43).unwrap();
        assert!(samples(file.path()).is_err());
    }
}
