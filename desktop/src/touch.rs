use gpui::{Bounds, Pixels, Point};
use serde_json::Value;

#[derive(Clone, Copy)]
pub struct Mapping {
    width: u32,
    height: u32,
    rotation: u32,
}

impl Mapping {
    pub fn from_descriptor(value: &Value) -> Option<Self> {
        let width = u32::try_from(value["width"].as_u64()?).ok()?;
        let height = u32::try_from(value["height"].as_u64()?).ok()?;
        let rotation = u32::try_from(value["rotation_clockwise"].as_u64().unwrap_or(0)).ok()?;
        (width > 0 && height > 0 && matches!(rotation, 0 | 90 | 180 | 270)).then_some(Self {
            width,
            height,
            rotation,
        })
    }

    pub fn position(self, bounds: Bounds<Pixels>, point: Point<Pixels>) -> (u32, u32) {
        let u = (point.x - bounds.origin.x) / bounds.size.width;
        let v = (point.y - bounds.origin.y) / bounds.size.height;
        let (display_width, display_height) = if self.rotation % 180 == 0 {
            (self.width, self.height)
        } else {
            (self.height, self.width)
        };
        let x = (u.clamp(0., 1.) * display_width as f32) as u32;
        let y = (v.clamp(0., 1.) * display_height as f32) as u32;
        let x = x.min(display_width - 1);
        let y = y.min(display_height - 1);
        match self.rotation {
            90 => (self.width - 1 - y, x),
            180 => (self.width - 1 - x, self.height - 1 - y),
            270 => (y, self.height - 1 - x),
            _ => (x, y),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use gpui::{point, px, size};

    #[test]
    fn evb_touch_matches_panel_mount_at_scaled_window_positions() {
        let board: Value =
            serde_json::from_str(include_str!("../../boards/arcs-evb.json")).unwrap();
        let board = lisem_core::board::compile(&board).unwrap();
        let mapping = Mapping::from_descriptor(&board["touch"]).unwrap();
        let bounds = Bounds::new(point(px(100.), px(200.)), size(px(640.), px(480.)));
        assert_eq!(
            mapping.position(bounds, point(px(100.), px(200.))),
            (0, 319)
        );
        assert_eq!(mapping.position(bounds, point(px(739.), px(200.))), (0, 0));
        assert_eq!(
            mapping.position(bounds, point(px(100.), px(679.))),
            (239, 319)
        );
        assert_eq!(
            mapping.position(bounds, point(px(739.), px(679.))),
            (239, 0)
        );
        assert_eq!(
            mapping.position(bounds, point(px(420.), px(440.))),
            (120, 159)
        );
        assert_eq!(
            mapping.position(bounds, point(px(900.), px(900.))),
            (239, 0)
        );
        assert_eq!(mapping.position(bounds, point(px(0.), px(0.))), (0, 319));
    }
}
