//! Bounded host image decoding; QEMU receives only one RGB888 sensor frame.
use anyhow::{Context, Result, ensure};
use image::{ImageReader, imageops::sample_bilinear};
use std::{
    fs::File,
    io::{BufReader, Read},
    path::Path,
};

pub const WIDTH: u32 = 640;
pub const HEIGHT: u32 = 480;
const MAX_FILE: u64 = 32 * 1024 * 1024;

/// Clockwise source-to-sensor rotation, before firmware crop/mirror/flip.
pub fn mounting_rotation(board: &serde_json::Value) -> Result<u16> {
    let rotation = board["camera"]["rotation_clockwise"]
        .as_u64()
        .context("Missing camera mounting rotation")?;
    ensure!(
        matches!(rotation, 0 | 90 | 180 | 270),
        "Invalid camera mounting rotation"
    );
    Ok(rotation as u16)
}

pub fn load(path: &Path, rotation: u16) -> Result<Vec<u8>> {
    let file = File::open(path).context("Cannot open camera image")?;
    ensure!(
        file.metadata()?.len() <= MAX_FILE,
        "Camera image exceeds 32 MiB"
    );
    let mut bytes = Vec::new();
    file.take(MAX_FILE + 1).read_to_end(&mut bytes)?;
    ensure!(
        bytes.len() as u64 <= MAX_FILE,
        "Camera image exceeds 32 MiB"
    );
    let mut reader =
        ImageReader::new(BufReader::new(std::io::Cursor::new(bytes))).with_guessed_format()?;
    ensure!(
        matches!(
            reader.format(),
            Some(image::ImageFormat::Png | image::ImageFormat::Jpeg | image::ImageFormat::Pnm)
        ),
        "Camera input requires PNG, JPEG or PNM"
    );
    let mut limits = image::Limits::default();
    limits.max_image_width = Some(4096);
    limits.max_image_height = Some(4096);
    limits.max_alloc = Some(128 * 1024 * 1024);
    reader.limits(limits);
    let image = reader
        .decode()
        .context("Cannot decode camera image (PNG, JPEG or PNM required)")?;
    ensure!(
        image.width() > 0 && image.height() > 0,
        "Camera image is empty"
    );
    adapt(&image.into_rgb8(), rotation)
}

/// Common still/live source adapter. The channel carries sensor-coordinate RGB;
/// mounting is applied here once, before GC0328's firmware-controlled transforms.
pub fn adapt(source: &image::RgbImage, rotation: u16) -> Result<Vec<u8>> {
    ensure!(
        source.width() > 0 && source.height() > 0,
        "Camera image is empty"
    );
    ensure!(
        matches!(rotation, 0 | 90 | 180 | 270),
        "Invalid camera mounting rotation"
    );
    let (width, height) = if matches!(rotation, 90 | 270) {
        (source.height(), source.width())
    } else {
        source.dimensions()
    };
    // Inverse-map the rotated, centered viewport directly into a fixed output.
    // This avoids both a second crop and a large rotated/resized allocation.
    let scale = (WIDTH as f32 / width as f32).max(HEIGHT as f32 / height as f32);
    let visible_x = WIDTH as f32 / scale / width as f32;
    let visible_y = HEIGHT as f32 / scale / height as f32;
    let frame = image::RgbImage::from_fn(WIDTH, HEIGHT, |x, y| {
        let u = 0.5 + ((x as f32 + 0.5) / WIDTH as f32 - 0.5) * visible_x;
        let v = 0.5 + ((y as f32 + 0.5) / HEIGHT as f32 - 0.5) * visible_y;
        let (u, v) = match rotation {
            90 => (v, 1.0 - u),
            180 => (1.0 - u, 1.0 - v),
            270 => (1.0 - v, u),
            _ => (u, v),
        };
        sample_bilinear(source, u, v).unwrap()
    });
    Ok(frame.into_raw())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn input_decodes_and_fills_sensor_frame() {
        let dir = tempfile::tempdir().unwrap();
        for extension in ["png", "jpg", "ppm"] {
            let path = dir.path().join(format!("input.{extension}"));
            image::RgbImage::from_pixel(8, 4, image::Rgb([70, 120, 200]))
                .save(&path)
                .unwrap();
            let bytes = load(&path, 0).unwrap();
            assert_eq!(bytes.len(), (WIDTH * HEIGHT * 3) as usize);
            assert!(bytes.chunks_exact(3).all(|p| p[0].abs_diff(70) < 3
                && p[1].abs_diff(120) < 3
                && p[2].abs_diff(200) < 3));
        }
    }
    #[test]
    fn wide_image_is_center_cropped_not_stretched() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("wide.png");
        let source = image::RgbImage::from_fn(1280, 480, |x, _| {
            if (320..960).contains(&x) {
                image::Rgb([0, 255, 0])
            } else {
                image::Rgb([255, 0, 0])
            }
        });
        source.save(&path).unwrap();
        let bytes = load(&path, 0).unwrap();
        assert!(bytes.chunks_exact(3).all(|p| p == [0, 255, 0]));
    }
    #[test]
    fn extreme_aspect_ratios_keep_fixed_output_geometry() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("thin.png");
        for (width, height) in [(4096, 1), (1, 4096), (1, 1)] {
            image::RgbImage::from_pixel(width, height, image::Rgb([20, 40, 60]))
                .save(&path)
                .unwrap();
            let bytes = load(&path, 0).unwrap();
            assert_eq!(bytes.len(), (WIDTH * HEIGHT * 3) as usize);
            assert!(bytes.chunks_exact(3).all(|p| p == [20, 40, 60]));
        }
    }
    #[test]
    fn mounting_rotates_asymmetric_pixels_before_a_single_crop() {
        // Portrait scene fits the landscape sensor exactly after a quarter turn.
        let colors = [[240, 10, 20], [30, 220, 40], [50, 60, 200], [230, 210, 70]];
        let source = image::RgbImage::from_fn(480, 640, |x, y| {
            image::Rgb(colors[(y >= 320) as usize * 2 + (x >= 240) as usize])
        });
        for (rotation, expected) in [
            (0, [0, 1, 2, 3]),
            (90, [2, 0, 3, 1]),
            (180, [3, 2, 1, 0]),
            (270, [1, 3, 0, 2]),
        ] {
            let frame = adapt(&source, rotation).unwrap();
            for ((x, y), color) in [(160, 120), (480, 120), (160, 360), (480, 360)]
                .into_iter()
                .zip(expected)
            {
                let offset = (y * WIDTH as usize + x) * 3;
                assert_eq!(
                    &frame[offset..offset + 3],
                    colors[color],
                    "rotation {rotation}"
                );
            }
        }
        // Crop in rotated geometry: only the middle 480 source columns remain.
        let source = image::RgbImage::from_fn(960, 640, |x, _| {
            image::Rgb(if (240..720).contains(&x) {
                [0, 255, 0]
            } else {
                [255, 0, 0]
            })
        });
        assert!(
            adapt(&source, 90)
                .unwrap()
                .chunks_exact(3)
                .all(|p| p == [0, 255, 0])
        );
        assert!(adapt(&source, 45).is_err());
    }

    #[test]
    fn mini_mounting_preserves_firmware_mirror_and_turns_preview_left() {
        // Non-symmetric square: compare Rcw Mx Rcw (new) with Rccw Rcw Mx (old).
        let scene = image::RgbImage::from_fn(640, 640, |x, y| {
            image::Rgb([(x / 4) as u8, (y / 4) as u8, ((x + 2 * y) / 8) as u8])
        });
        let sensor = image::RgbImage::from_raw(WIDTH, HEIGHT, adapt(&scene, 90).unwrap()).unwrap();
        let preview = image::imageops::rotate90(&image::imageops::flip_horizontal(&sensor));
        // A square preview uses the centered square sensor region, so compare its
        // center with the source's horizontal mirror (firmware mirror retained).
        for (x, y) in [(100, 100), (350, 100), (100, 350), (350, 350)] {
            let p = preview.get_pixel(x, y + 80);
            let expected = scene.get_pixel(639 - (x + 80), y + 80);
            for c in 0..3 {
                assert!(p[c].abs_diff(expected[c]) <= 1);
            }
        }
    }

    #[test]
    fn invalid_and_oversized_inputs_fail() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("input.png");
        std::fs::write(&path, b"not an image").unwrap();
        assert!(load(&path, 0).is_err());
        File::create(&path).unwrap().set_len(MAX_FILE + 1).unwrap();
        assert!(load(&path, 0).unwrap_err().to_string().contains("32 MiB"));
        image::RgbImage::new(4097, 1).save(&path).unwrap();
        assert!(load(&path, 0).is_err());
        assert!(load(&dir.path().join("missing.png"), 0).is_err());
    }
}
