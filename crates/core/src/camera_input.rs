//! A source-independent, bounded latest-frame channel. Still images and future
//! live producers share slots; only changing sources needs a QMP transaction.
//! Pixels are in sensor coordinates: all producers must first use camera::adapt
//! with the board mounting rotation. GC0328 crop/mirror/flip remain downstream.
use crate::shared::Mapping;
use anyhow::{Context, Result, ensure};
use std::sync::atomic::{AtomicU64, Ordering};

const MAGIC: u64 = 0x4c495343414d3031;
const HEADER: usize = 128;
const METADATA: usize = 40;
pub const WIDTH: usize = 640;
pub const HEIGHT: usize = 480;
pub const BYTES: usize = WIDTH * HEIGHT * 3;
const SLOT: usize = METADATA + BYTES;
const LENGTH: usize = HEADER + 3 * SLOT;

/// Transport state of the source, independent of the sensor's SCCB registers.
#[allow(dead_code)] // Live producers use this same ABI without per-frame QMP.
#[derive(Clone, Copy)]
#[repr(u64)]
pub enum SourceState {
    Clear = 0,
    Still = 1,
    Live = 2,
    Disconnected = 3,
}

pub struct Input {
    map: Mapping,
    sequence: u64,
    generation: u64,
    active: u64,
    active_sequence: u64,
    active_frame: u64,
    pending: Option<(u64, u64, u64)>,
    active_publication: u64,
}
impl Input {
    pub fn open(name: &str) -> Result<Self> {
        let name = name
            .strip_prefix("shm:")
            .ok_or_else(|| anyhow::anyhow!("Camera input requires named shared memory"))?;
        let map = Mapping::open(name, LENGTH)?;
        Self::from_mapping(map)
    }
    fn from_mapping(map: Mapping) -> Result<Self> {
        let input = Self {
            map,
            sequence: 0,
            generation: 0,
            active: 0,
            active_sequence: 0,
            active_frame: 0,
            pending: None,
            active_publication: 0,
        };
        ensure!(
            input.atomic(0).load(Ordering::Acquire) == MAGIC,
            "Camera input map is not ready"
        );
        for (index, expected) in [
            (1, WIDTH as u64),
            (2, HEIGHT as u64),
            (3, WIDTH as u64 * 3),
            (4, BYTES as u64),
        ] {
            ensure!(
                input.atomic(index).load(Ordering::Relaxed) == expected,
                "Invalid camera input geometry"
            );
        }
        Ok(input)
    }
    fn atomic(&self, index: usize) -> &AtomicU64 {
        unsafe { &*self.map.as_ptr().add(index * 8).cast::<AtomicU64>() }
    }
    fn frame(
        &mut self,
        generation: u64,
        frame_sequence: u64,
        frame_index: u64,
        state: SourceState,
        rgb: Option<&[u8]>,
        host_ns: u64,
    ) -> Result<Option<u64>> {
        ensure!(
            match state {
                SourceState::Still | SourceState::Live =>
                    rgb.is_some_and(|data| data.len() == BYTES),
                _ => rgb.is_none(),
            },
            "Camera source frame must contain exactly 640x480 RGB888 pixels, or no pixels when unavailable"
        );
        let sequence = self
            .sequence
            .checked_add(1)
            .filter(|value| *value < (1_u64 << 62))
            .ok_or_else(|| anyhow::anyhow!("Camera frame sequence exhausted"))?;
        for slot in 0..3 {
            // Pin a pending source's first frame until its QMP outcome is
            // confirmed. The active source can keep alternating the other
            // two slots; never overwrite its latest complete publication.
            if (self.active_publication != 0 && self.active_publication & 3 == slot as u64)
                || self
                    .pending
                    .is_some_and(|(_, publication, _)| publication & 3 == slot as u64)
            {
                continue;
            }
            if self
                .atomic(6 + slot)
                .compare_exchange(0, 1, Ordering::AcqRel, Ordering::Acquire)
                .is_err()
            {
                continue;
            }
            let start = HEADER + slot * SLOT;
            let pointer = unsafe { self.map.as_mut_ptr().add(start) };
            let metadata = [
                generation,
                frame_sequence,
                host_ns,
                state as u64,
                frame_index,
            ];
            unsafe {
                std::ptr::copy_nonoverlapping(metadata.as_ptr().cast::<u8>(), pointer, METADATA);
                if let Some(rgb) = rgb {
                    std::ptr::copy_nonoverlapping(rgb.as_ptr(), pointer.add(METADATA), BYTES);
                }
            }
            self.atomic(6 + slot).store(0, Ordering::Release);
            let publication = (sequence << 2) | slot as u64;
            self.atomic(5).store(publication, Ordering::Release);
            self.sequence = sequence;
            return Ok(Some(publication));
        }
        self.atomic(10).fetch_add(1, Ordering::Relaxed);
        Ok(None)
    }
    pub fn prepare(&mut self, state: SourceState, rgb: Option<&[u8]>, host_ns: u64) -> Result<u64> {
        ensure!(
            self.pending.is_none(),
            "Camera source selection is still pending"
        );
        let generation = self
            .generation
            .checked_add(1)
            .ok_or_else(|| anyhow::anyhow!("Camera source generation exhausted"))?;
        let publication = self
            .frame(generation, 1, u64::from(rgb.is_some()), state, rgb, host_ns)?
            .context("Camera input slots are busy; source was not changed")?;
        self.generation = generation;
        self.pending = Some((generation, publication, u64::from(rgb.is_some())));
        Ok(generation)
    }
    /// Only a definitive reply resolves source selection; a timeout must retain
    /// the first frame. Subsequent media publications need no QMP round trip.
    pub fn confirmed(&mut self, generation: u64, applied: bool) -> Result<()> {
        ensure!(
            self.pending
                .is_some_and(|(pending, _, _)| pending == generation),
            "Camera source confirmation belongs to an earlier generation"
        );
        let (_, publication, frame) = self.pending.take().unwrap();
        if applied {
            self.active = generation;
            self.active_sequence = 1;
            self.active_frame = frame;
            self.active_publication = publication;
        }
        Ok(())
    }
    #[allow(dead_code)] // The media channel is ready for a future live source.
    pub fn publish(
        &mut self,
        rgb: Option<&[u8]>,
        state: SourceState,
        host_ns: u64,
    ) -> Result<bool> {
        ensure!(self.active != 0, "Camera source is not selected");
        let sequence = self
            .active_sequence
            .checked_add(1)
            .ok_or_else(|| anyhow::anyhow!("Camera source frame sequence exhausted"))?;
        let frame = self
            .active_frame
            .checked_add(u64::from(rgb.is_some()))
            .context("Camera frame index exhausted")?;
        if let Some(publication) = self.frame(self.active, sequence, frame, state, rgb, host_ns)? {
            self.active_publication = publication;
            self.active_sequence = sequence;
            self.active_frame = frame;
            Ok(true)
        } else {
            Ok(false)
        }
    }
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    fn input() -> Input {
        let mut map = Mapping::anonymous(LENGTH);
        let header = [
            MAGIC,
            WIDTH as u64,
            HEIGHT as u64,
            (WIDTH * 3) as u64,
            BYTES as u64,
        ];
        unsafe {
            std::ptr::copy_nonoverlapping(header.as_ptr().cast::<u8>(), map.as_mut_ptr(), 40);
        }
        Input::from_mapping(map).unwrap()
    }
    fn frame(input: &Input, publication: u64) -> (Vec<u64>, Vec<u8>) {
        let slot = (publication & 3) as usize;
        assert!(
            input
                .atomic(6 + slot)
                .compare_exchange(0, 2, Ordering::AcqRel, Ordering::Acquire)
                .is_ok()
        );
        let start = HEADER + slot * SLOT;
        let metadata = input.map[start..start + METADATA]
            .chunks_exact(8)
            .map(|bytes| u64::from_ne_bytes(bytes.try_into().unwrap()))
            .collect();
        let pixels = input.map[start + METADATA..start + SLOT].to_vec();
        input.atomic(6 + slot).store(0, Ordering::Release);
        (metadata, pixels)
    }
    #[test]
    fn pending_source_pins_first_frame_while_active_source_keeps_streaming() {
        let mut input = input();
        let red = vec![11; BYTES];
        let blue = vec![22; BYTES];
        let first = input.prepare(SourceState::Live, Some(&red), 1).unwrap();
        input.confirmed(first, true).unwrap();
        let pending = input.prepare(SourceState::Still, Some(&blue), 2).unwrap();
        let pinned = input.pending.unwrap().1;
        assert!(input.prepare(SourceState::Clear, None, 0).is_err());
        for clock in 3..30 {
            assert!(input.publish(Some(&red), SourceState::Live, clock).unwrap());
            let (metadata, pixels) = frame(&input, input.active_publication);
            assert_eq!(metadata[0], first);
            assert_eq!(metadata[2], clock);
            assert_eq!(pixels, red);
            let (metadata, pixels) = frame(&input, pinned);
            assert_eq!(metadata[0], pending);
            assert_eq!(metadata[1], 1);
            assert_eq!(pixels, blue);
        }
        assert!(input.confirmed(first, true).is_err());
        input.confirmed(pending, false).unwrap();
        assert_eq!(input.active, first);
        assert!(input.publish(None, SourceState::Disconnected, 31).unwrap());
        let (metadata, _) = frame(&input, input.active_publication);
        assert_eq!(metadata[3], 3);
        assert_eq!(metadata[4], 28); // Unavailable markers do not count as pixels.
        assert!(input.publish(Some(&red), SourceState::Live, 32).unwrap());
        assert_eq!(frame(&input, input.active_publication).0[4], 29);
        let clear = input.prepare(SourceState::Clear, None, 0).unwrap();
        assert!(clear > pending);
        input.confirmed(clear, true).unwrap();
        assert_eq!(frame(&input, input.active_publication).0[3], 0);
    }
    #[test]
    fn producer_respects_reader_ownership_and_counts_bounded_drops() {
        let mut input = input();
        let rgb = vec![44; BYTES];
        let first = input.prepare(SourceState::Live, Some(&rgb), 1).unwrap();
        input.confirmed(first, true).unwrap();
        for slot in 0..3 {
            input.atomic(6 + slot).store(2, Ordering::Release);
        }
        assert!(!input.publish(Some(&rgb), SourceState::Live, 2).unwrap());
        assert_eq!(input.atomic(10).load(Ordering::Acquire), 1);
        assert_eq!(input.active_sequence, 1);
        for slot in 0..3 {
            input.atomic(6 + slot).store(0, Ordering::Release);
        }
        assert!(input.publish(Some(&rgb), SourceState::Live, 3).unwrap());
        assert!(input.publish(None, SourceState::Live, 4).is_err());
        assert!(
            input
                .prepare(SourceState::Still, Some(&rgb[..BYTES - 1]), 5)
                .is_err()
        );
        assert!(input.pending.is_none());
    }
}
