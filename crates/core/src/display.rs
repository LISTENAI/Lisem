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
    map: Map,
    width: u32,
    height: u32,
    bytes: usize,
    last: u64,
}

enum Map {
    File(memmap2::MmapMut),
    Shared(crate::shared::Mapping),
}
impl std::ops::Deref for Map {
    type Target = [u8];
    fn deref(&self) -> &[u8] {
        match self {
            Self::File(map) => map,
            Self::Shared(map) => map,
        }
    }
}

impl Display {
    pub fn open(path: &Path) -> Result<Self> {
        let map = if let Some(name) = path.to_str().and_then(|s| s.strip_prefix("shm:")) {
            let header = crate::shared::Mapping::open(name, HEADER)?;
            let magic = unsafe { &*header.as_ptr().cast::<AtomicU64>() };
            if magic.load(Ordering::Acquire) != MAGIC {
                anyhow::bail!("Display map is not ready");
            }
            let bytes = u64::from_ne_bytes(header[32..40].try_into().unwrap());
            anyhow::ensure!(
                (4..=2048 * 2048 * 4).contains(&bytes),
                "Invalid display map size"
            );
            let len = HEADER + 3 * (16 + bytes as usize);
            Map::Shared(crate::shared::Mapping::open(name, len)?)
        } else {
            let file = OpenOptions::new().read(true).write(true).open(path)?;
            let len = file.metadata()?.len() as usize;
            anyhow::ensure!(
                (HEADER + 3 * 20..=HEADER + 3 * (16 + 2048 * 2048 * 4)).contains(&len),
                "Invalid display map size"
            );
            Map::File(unsafe { memmap2::MmapOptions::new().map_mut(&file)? })
        };
        let len = map.len();
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
        // Clocks and pixels belong to the same publication and must all be
        // copied while this slot is exclusively owned by the reader.
        let virtual_ns = u64::from_ne_bytes(self.map[start - 16..start - 8].try_into().unwrap());
        let host_monotonic_ns = u64::from_ne_bytes(self.map[start - 8..start].try_into().unwrap());
        let pixels =
            unsafe { std::slice::from_raw_parts(self.map.as_ptr().add(start), self.bytes) }
                .to_vec();
        self.atomic(6 + slot).store(0, Ordering::Release);
        self.last = publication;
        Some(Frame {
            width: self.width,
            height: self.height,
            bgra: pixels,
            sequence: publication >> 2,
            virtual_ns,
            host_monotonic_ns,
        })
    }
}

#[derive(Clone)]
pub struct Frame {
    pub width: u32,
    pub height: u32,
    pub bgra: Vec<u8>,
    pub sequence: u64,
    pub virtual_ns: u64,
    pub host_monotonic_ns: u64,
}
impl Frame {
    pub fn save(&self, path: &Path) -> Result<()> {
        std::fs::write(path, self.png()?)?;
        Ok(())
    }
    pub fn png(&self) -> Result<Vec<u8>> {
        let mut rgba = self.bgra.clone();
        for pixel in rgba.chunks_exact_mut(4) {
            pixel.swap(0, 2);
        }
        let mut png = std::io::Cursor::new(Vec::new());
        image::RgbaImage::from_raw(self.width, self.height, rgba)
            .context("Invalid frame geometry")?
            .write_to(&mut png, image::ImageFormat::Png)?;
        Ok(png.into_inner())
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
        for (virtual_ns, host_ns, pixel) in [(123_u64, 456_u64, 0x11), (789, 999, 0x22), (0, 0, 0)]
        {
            file.write_all(&virtual_ns.to_ne_bytes()).unwrap();
            file.write_all(&host_ns.to_ne_bytes()).unwrap();
            file.write_all(&[pixel; 4]).unwrap();
        }
        let mut display = Display::open(&path).unwrap();
        let frame = display.latest().unwrap();
        assert_eq!(
            (frame.sequence, frame.virtual_ns, frame.host_monotonic_ns),
            (1, 123, 456)
        );
        assert_eq!(frame.bgra, [0x11; 4]);
        assert!(display.latest().is_none());
        // Producer owns slot 1, so it cannot be read, even if announced.
        display.atomic(7).store(1, Ordering::Release);
        display.atomic(5).store(9, Ordering::Release);
        assert!(display.latest().is_none());
        assert_eq!(display.last, 4);
        display.atomic(7).store(0, Ordering::Release);
        let frame = display.latest().unwrap();
        assert_eq!(
            (frame.sequence, frame.virtual_ns, frame.host_monotonic_ns),
            (2, 789, 999)
        );
        assert_eq!(frame.bgra, [0x22; 4]);
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
