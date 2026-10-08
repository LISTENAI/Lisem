//! GPUI rendering adapter for the shared headless display transport.
use gpui::RenderImage;
use std::{
    path::{Path, PathBuf},
    sync::Arc,
};

pub struct Display {
    pub path: PathBuf,
    transport: lisem_core::display::Display,
}

impl Display {
    pub fn open(path: &Path) -> Result<Self, String> {
        let transport =
            lisem_core::display::Display::open(path).map_err(|error| error.to_string())?;
        Ok(Self {
            path: path.to_owned(),
            transport,
        })
    }

    pub fn next(&mut self) -> Option<Arc<RenderImage>> {
        let frame = self.transport.latest()?;
        let image = image::RgbaImage::from_raw(frame.width, frame.height, frame.bgra)?;
        Some(Arc::new(RenderImage::new(smallvec::smallvec![
            image::Frame::new(image)
        ])))
    }
}
