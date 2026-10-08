use gpui::{AssetSource, SharedString};
use std::borrow::Cow;

pub struct Assets;

impl AssetSource for Assets {
    fn load(&self, path: &str) -> gpui::Result<Option<Cow<'static, [u8]>>> {
        let bytes: &'static [u8] = match path {
            "app-icon" => include_bytes!("../assets/app/icon-256.png"),
            "power" => include_bytes!("../assets/power.svg"),
            "reset" => include_bytes!("../assets/reset.svg"),
            "speaker" => include_bytes!("../assets/speaker.svg"),
            "mute" => include_bytes!("../assets/mute.svg"),
            "serial" => include_bytes!("../assets/serial.svg"),
            "more" => include_bytes!("../assets/more.svg"),
            "audio" => include_bytes!("../assets/audio.svg"),
            "network" => include_bytes!("../assets/network.svg"),
            _ => return gpui::assets::Assets.load(path),
        };
        Ok(Some(Cow::Borrowed(bytes)))
    }

    fn list(&self, path: &str) -> gpui::Result<Vec<SharedString>> {
        gpui::assets::Assets.list(path)
    }
}
