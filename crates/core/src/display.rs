//! Latest-frame transport from a QEMU DisplayChangeListener. Each slot has
//! exclusive ownership; the producer never waits for a slow or hidden window.
use anyhow::{Context, Result};
use std::{
    fs::OpenOptions,
    path::{Path, PathBuf},
    sync::atomic::{AtomicU64, Ordering},
};

const MAGIC: u64 = 0x4c49534144495331;
const HEADER: usize = 80;

pub struct Display {
    pub path: PathBuf,
    map: memmap2::MmapMut,
    width: u32,
    height: u32,
    bytes: usize,
    last: u64,
}

impl Display {
    pub fn open(path: &Path) -> Result<Self> {
        let file = OpenOptions::new().read(true).write(true).open(path)?;
        let len = file.metadata()?.len() as usize;
        if !(HEADER + 3 * 20..=HEADER + 3 * (16 + 2048 * 2048 * 4)).contains(&len) {
            return Err(anyhow::anyhow!("Invalid display map size"));
        }
        let map = unsafe { memmap2::MmapOptions::new().map_mut(&file)? };
        let mut display = Self {
            path: path.to_owned(),
            map,
            width: 0,
            height: 0,
            bytes: 0,
            last: 0,
        };
        // Header geometry is immutable after release-publication of the magic.
        if display.atomic(0).load(Ordering::Acquire) != MAGIC {
            return Err(anyhow::anyhow!("Display map is not ready"));
        }
        let width = display.atomic(1).load(Ordering::Relaxed);
        let height = display.atomic(2).load(Ordering::Relaxed);
        let stride = display.atomic(3).load(Ordering::Relaxed);
        let bytes = display.atomic(4).load(Ordering::Relaxed);
        if !(1..=2048).contains(&width)
            || !(1..=2048).contains(&height)
            || stride != width * 4
            || bytes != stride * height
            || HEADER as u64 + 3 * (16 + bytes) != len as u64
        {
            return Err(anyhow::anyhow!("Invalid display geometry"));
        }
        display.width = width as u32;
        display.height = height as u32;
        display.bytes = bytes as usize;
        Ok(display)
    }

    fn atomic(&self, index: usize) -> &AtomicU64 {
        unsafe { &*self.map.as_ptr().add(index * 8).cast::<AtomicU64>() }
    }

    pub fn latest(&mut self) -> Option<Frame> {
        let publication = self.atomic(5).load(Ordering::Acquire);
        if publication == 0 || publication == self.last {
            return None;
        }
        let slot = (publication & 3) as usize;
        if slot >= 3
            || self
                .atomic(6 + slot)
                .compare_exchange(0, 2, Ordering::AcqRel, Ordering::Acquire)
                .is_err()
        {
            return None;
        }
        // A newer publication may have reused this slot before it was locked.
        if self.atomic(5).load(Ordering::Acquire) != publication {
            self.atomic(6 + slot).store(0, Ordering::Release);
            return None;
        }
        let start = HEADER + slot * (16 + self.bytes) + 16;
        let pixels =
            unsafe { std::slice::from_raw_parts(self.map.as_ptr().add(start), self.bytes) }
                .to_vec();
        self.atomic(6 + slot).store(0, Ordering::Release);
        self.last = publication;
        Some(Frame {
            width: self.width,
            height: self.height,
            bgra: pixels,
        })
    }
}

pub struct Frame {
    pub width: u32,
    pub height: u32,
    pub bgra: Vec<u8>,
}
impl Frame {
    pub fn save(&self, path: &Path) -> Result<()> {
        let mut rgba = self.bgra.clone();
        for pixel in rgba.chunks_exact_mut(4) {
            pixel.swap(0, 2);
        }
        image::RgbaImage::from_raw(self.width, self.height, rgba)
            .context("Invalid frame geometry")?
            .save_with_format(path, image::ImageFormat::Png)?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{Seek, SeekFrom, Write};

    #[test]
    fn shared_frames_require_exclusive_ownership_and_new_publication() {
        let path = std::env::temp_dir().join(format!("lisa-display-test-{}", std::process::id()));
        let mut file = OpenOptions::new()
            .create_new(true)
            .write(true)
            .open(&path)
            .unwrap();
        let words: [u64; 10] = [MAGIC, 1, 1, 4, 4, 4, 0, 0, 0, 0];
        for value in words {
            file.write_all(&value.to_ne_bytes()).unwrap();
        }
        file.write_all(&[0; 60]).unwrap();
        let mut display = Display::open(&path).unwrap();
        assert!(display.latest().is_some());
        assert!(display.latest().is_none());
        // Producer owns slot 1, so it cannot be read, even if announced.
        display.atomic(7).store(1, Ordering::Release);
        display.atomic(5).store(9, Ordering::Release);
        assert!(display.latest().is_none());
        assert_eq!(display.last, 4);
        display.atomic(7).store(0, Ordering::Release);
        assert!(display.latest().is_some());
        assert_eq!(display.atomic(7).load(Ordering::Acquire), 0);
        // Invalid geometry must be rejected without touching any slot.
        file.seek(SeekFrom::Start(8)).unwrap();
        file.write_all(&2049_u64.to_ne_bytes()).unwrap();
        assert!(Display::open(&path).is_err());
        drop(display);
        drop(file);
        std::fs::remove_file(path).unwrap();
    }
}
