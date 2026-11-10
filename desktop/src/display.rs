//! GPUI rendering adapter for the shared headless display transport.
use gpui::RenderImage;
use std::{
    path::{Path, PathBuf},
    sync::Arc,
};

impl crate::Desktop {
    pub(super) fn display_scale_menu(
        &self,
        cx: &mut gpui::Context<Self>,
    ) -> impl gpui::IntoElement {
        use crate::*;
        let selected = self.display_scale;
        let view = cx.entity().downgrade();
        Button::new("display-scale")
            .ghost()
            .label(
                selected
                    .map(|v| format!("{v}%"))
                    .unwrap_or_else(|| "适应窗口".into()),
            )
            .accessibility_label("显示倍率")
            .tooltip("显示倍率")
            .dropdown_menu(move |mut menu, _, _| {
                for value in [
                    None,
                    Some(50),
                    Some(75),
                    Some(100),
                    Some(150),
                    Some(200),
                    Some(300),
                    Some(400),
                ] {
                    let weak = view.clone();
                    let label = value
                        .map(|v| format!("{v}%"))
                        .unwrap_or_else(|| "适应窗口".into());
                    menu = menu.item(
                        PopupMenuItem::new(label)
                            .checked(value == selected)
                            .on_click(move |_, _, cx| {
                                let _ = weak.update(cx, |v, cx| {
                                    v.release_touch();
                                    v.display_scale = value;
                                    cx.notify();
                                });
                            }),
                    );
                }
                menu
            })
    }
}

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
