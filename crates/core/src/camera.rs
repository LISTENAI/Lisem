//! Bounded host image decoding; QEMU receives only one RGB888 sensor frame.
use anyhow::{Context, Result, ensure};
use base64::Engine;
use image::{ImageReader, imageops::sample_bilinear};
use std::{
    fs::File,
    io::{BufReader, Read},
    path::Path,
};

pub const WIDTH: u32 = 640;
pub const HEIGHT: u32 = 480;
const MAX_FILE: u64 = 32 * 1024 * 1024;

pub fn load(path: &Path) -> Result<String> {
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
    // Sample the centered viewport directly into a fixed output. Resizing the
    // entire image before cropping can allocate gigabytes for a 4096x1 input.
    let source = image.into_rgb8();
    let scale = (WIDTH as f32 / source.width() as f32).max(HEIGHT as f32 / source.height() as f32);
    let visible_x = WIDTH as f32 / scale / source.width() as f32;
    let visible_y = HEIGHT as f32 / scale / source.height() as f32;
    let frame = image::RgbImage::from_fn(WIDTH, HEIGHT, |x, y| {
        let u = 0.5 + ((x as f32 + 0.5) / WIDTH as f32 - 0.5) * visible_x;
        let v = 0.5 + ((y as f32 + 0.5) / HEIGHT as f32 - 0.5) * visible_y;
        // A nonempty source and pixel-center coordinates guarantee valid samples.
        sample_bilinear(&source, u, v).unwrap()
    });
    Ok(base64::engine::general_purpose::STANDARD.encode(frame.as_raw()))
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
            let bytes = base64::engine::general_purpose::STANDARD
                .decode(load(&path).unwrap())
                .unwrap();
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
        let bytes = base64::engine::general_purpose::STANDARD
            .decode(load(&path).unwrap())
            .unwrap();
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
            let bytes = base64::engine::general_purpose::STANDARD
                .decode(load(&path).unwrap())
                .unwrap();
            assert_eq!(bytes.len(), (WIDTH * HEIGHT * 3) as usize);
            assert!(bytes.chunks_exact(3).all(|p| p == [20, 40, 60]));
        }
    }
    #[test]
    fn invalid_and_oversized_inputs_fail() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("input.png");
        std::fs::write(&path, b"not an image").unwrap();
        assert!(load(&path).is_err());
        File::create(&path).unwrap().set_len(MAX_FILE + 1).unwrap();
        assert!(load(&path).unwrap_err().to_string().contains("32 MiB"));
        image::RgbImage::new(4097, 1).save(&path).unwrap();
        assert!(load(&path).is_err());
        assert!(load(&dir.path().join("missing.png")).is_err());
    }
}
