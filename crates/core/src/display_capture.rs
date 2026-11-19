//! Explicit append-only display capture; pixels never travel through QMP.
use anyhow::{Context, Result, ensure};
use base64::Engine;
use serde_json::{Value, json};
use std::sync::atomic::{AtomicU64, Ordering};

const HEADER: usize = 128;
const MAGIC: u64 = 0x4c53444341503031;
pub const LIMIT: usize = 64 * 1024 * 1024;

pub struct Capture {
    pub id: String,
    pub run: Value,
    map: Option<crate::shared::Mapping>,
    width: usize,
    height: usize,
    capacity: usize,
}

impl Capture {
    pub fn pending(id: String, run: Value) -> Self {
        Self {
            id,
            run,
            map: None,
            width: 0,
            height: 0,
            capacity: 0,
        }
    }

    pub fn open(&mut self, width: usize, height: usize, capacity: usize) -> Result<()> {
        ensure!(
            (1..=2048).contains(&width)
                && (1..=2048).contains(&height)
                && (1..=4096).contains(&capacity),
            "Invalid display capture geometry"
        );
        let bytes = width * height * 4;
        let size = HEADER + capacity * (24 + bytes);
        ensure!(size <= LIMIT, "Display capture exceeds 64 MiB");
        let map = crate::shared::Mapping::open(
            self.id
                .strip_prefix("shm:")
                .context("Invalid capture identity")?,
            size,
        )?;
        let word = |index: usize| {
            unsafe { &*map.as_ptr().add(index * 8).cast::<AtomicU64>() }.load(Ordering::Acquire)
        };
        ensure!(
            word(0) == MAGIC
                && word(1) == width as u64
                && word(2) == height as u64
                && word(3) == bytes as u64
                && word(4) == capacity as u64,
            "Invalid display capture header"
        );
        self.width = width;
        self.height = height;
        self.capacity = capacity;
        self.map = Some(map);
        Ok(())
    }

    fn word(&self, index: usize) -> Result<u64> {
        let map = self
            .map
            .as_ref()
            .context("Display capture start was not confirmed")?;
        Ok(unsafe { &*map.as_ptr().add(index * 8).cast::<AtomicU64>() }.load(Ordering::Acquire))
    }

    pub fn status(&self, running: bool) -> Result<Value> {
        // Terminal state release-publishes the end boundary. A completed record
        // is immutable even while later records are being appended.
        let state = self.word(6)?;
        let count = self.word(5)?;
        ensure!(
            count <= self.capacity as u64,
            "Invalid display capture count"
        );
        let status = match state {
            1 if running => "running",
            1 => "interrupted",
            2 => "completed",
            3 => "overflow",
            4 => "expired",
            5 => "interrupted",
            _ => anyhow::bail!("Invalid display capture state"),
        };
        Ok(json!({"capture":self.id,"run":self.run,"status":status,
            "width":self.width,"height":self.height,"pixel_format":"bgra8",
            "capacity":self.capacity,"frames":count,"start_sequence":self.word(7)?,
            "started_ns":self.word(8)?,
            "end_sequence":if state == 1 { None } else { Some(self.word(9)?) },
            "ended_ns":if state == 1 { None } else { Some(self.word(10)?) }}))
    }

    pub fn read(&self, index: usize, offset: usize, limit: usize, running: bool) -> Result<Value> {
        let mut result = self.status(running)?;
        ensure!(
            index < result["frames"].as_u64().unwrap() as usize,
            "Frame is not captured"
        );
        let bytes = self.width * self.height * 4;
        ensure!(
            offset < bytes && (1..=1024 * 1024).contains(&limit),
            "Invalid frame byte range"
        );
        let map = self.map.as_ref().unwrap();
        let start = HEADER + index * (24 + bytes);
        let word = |n: usize| {
            u64::from_ne_bytes(map[start + n * 8..start + (n + 1) * 8].try_into().unwrap())
        };
        let end = (offset + limit).min(bytes);
        result["index"] = json!(index);
        result["frame"] =
            json!({"sequence":word(0),"virtual_ns":word(1),"host_monotonic_ns":word(2)});
        result["offset"] = json!(offset);
        result["next_offset"] = json!(end);
        result["bytes"] = json!(bytes);
        result["data"] = json!(
            base64::engine::general_purpose::STANDARD
                .encode(&map[start + 24 + offset..start + 24 + end])
        );
        Ok(result)
    }
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;

    fn fixture(state: u64) -> Capture {
        let mut map = crate::shared::Mapping::anonymous(HEADER + 32);
        let words = [
            MAGIC,
            2,
            1,
            8,
            1,
            1,
            state,
            40,
            90,
            41,
            100,
            0,
            0,
            0,
            0,
            0,
            41,
            100,
            500,
            0xff00ff00ffff0000,
        ];
        for (index, word) in words.into_iter().enumerate() {
            unsafe {
                map.as_mut_ptr().add(index * 8).cast::<u64>().write(word);
            }
        }
        Capture {
            id: "capture".into(),
            run: json!("run"),
            map: Some(map),
            width: 2,
            height: 1,
            capacity: 1,
        }
    }

    #[test]
    fn immutable_frame_chunks_keep_publication_metadata() {
        let capture = fixture(2);
        let first = capture.read(0, 0, 4, true).unwrap();
        let last = capture.read(0, 4, 4, true).unwrap();
        assert_eq!(
            first["frame"],
            json!({"sequence":41,"virtual_ns":100,"host_monotonic_ns":500})
        );
        assert_eq!(first["frame"], last["frame"]);
        assert_eq!(first["next_offset"], 4);
        assert_eq!(last["next_offset"], 8);
        assert_eq!(
            base64::engine::general_purpose::STANDARD
                .decode(first["data"].as_str().unwrap())
                .unwrap(),
            [0, 0, 255, 255]
        );
        assert!(capture.read(1, 0, 4, true).is_err());
        assert!(capture.read(0, 8, 4, true).is_err());
        assert!(capture.read(0, 0, 1024 * 1024 + 1, true).is_err());
    }

    #[test]
    fn terminal_and_interrupted_states_are_distinct() {
        for (state, expected) in [
            (2, "completed"),
            (3, "overflow"),
            (4, "expired"),
            (5, "interrupted"),
        ] {
            assert_eq!(fixture(state).status(false).unwrap()["status"], expected);
        }
        let capture = fixture(1);
        assert_eq!(capture.status(true).unwrap()["status"], "running");
        assert_eq!(capture.status(false).unwrap()["status"], "interrupted");
        assert!(capture.status(false).unwrap()["ended_ns"].is_null());
        assert!(fixture(9).status(true).is_err());
    }
}
