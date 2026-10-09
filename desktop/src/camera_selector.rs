use gpui::{SharedString, component::select::SelectItem};
use serde_json::Value;
use std::path::Path;

#[derive(Clone, PartialEq)]
pub enum CameraChoice {
    Off,
    Image,
    Device(String),
    Refresh,
}

#[derive(Clone, PartialEq)]
pub struct CameraOption {
    choice: CameraChoice,
    label: SharedString,
}

impl CameraOption {
    fn new(choice: CameraChoice, label: impl Into<SharedString>) -> Self {
        Self {
            choice,
            label: label.into(),
        }
    }
}

impl SelectItem for CameraOption {
    type Value = CameraChoice;

    fn title(&self) -> SharedString {
        self.label.clone()
    }
    fn value(&self) -> &CameraChoice {
        &self.choice
    }
}

pub fn options(
    host: &Value,
    cameras: &Value,
    supported: bool,
) -> (Vec<CameraOption>, CameraChoice) {
    let selected_device = host["camera_device"].as_str();
    let image = host["camera_image"].as_str();
    let selected = if let Some(id) = selected_device {
        CameraChoice::Device(id.into())
    } else if image.is_some() {
        CameraChoice::Image
    } else {
        CameraChoice::Off
    };
    let image_label = image
        .and_then(|path| Path::new(path).file_name())
        .map(|name| format!("图片 · {}…", name.to_string_lossy()))
        .unwrap_or_else(|| "图片…".into());
    let mut options = vec![
        CameraOption::new(CameraChoice::Off, "关闭输入"),
        CameraOption::new(CameraChoice::Image, image_label),
    ];
    if supported {
        for device in cameras["devices"].as_array().into_iter().flatten() {
            let Some(id) = device["id"].as_str() else {
                continue;
            };
            options.push(CameraOption::new(
                CameraChoice::Device(id.into()),
                device["name"].as_str().unwrap_or(id).to_owned(),
            ));
        }
    }
    // Preserve the selected identity when enumeration no longer finds the device.
    if let Some(id) = selected_device {
        if !options.iter().any(|option| option.choice == selected) {
            options.push(CameraOption::new(
                selected.clone(),
                format!("{}（未连接）", id),
            ));
        }
    }
    if supported {
        options.push(CameraOption::new(CameraChoice::Refresh, "刷新设备…"));
    }
    (options, selected)
}
