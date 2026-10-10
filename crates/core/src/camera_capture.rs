//! Native capture stays off the runtime thread. A single latest-frame mailbox
//! bounds memory and lets a slow guest drop host frames without changing time.
use crate::{assets::Assets, camera};
use anyhow::{Context, Result, bail, ensure};
use serde_json::{Value, json};
use std::{
    io::Read,
    process::{Child, Stdio},
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
    thread::{self, JoinHandle},
    time::{Duration, Instant},
};

const MAX_WIDTH: u32 = 1920;
const MAX_HEIGHT: u32 = 1080;
const MAX_ERROR: usize = 4096;
const ERROR_DRAIN_GRACE: Duration = Duration::from_millis(100);
pub const FIRST_FRAME_TIMEOUT: Duration = Duration::from_secs(120);

pub fn devices(assets: &Assets) -> Result<Value> {
    if !cfg!(target_os = "macos") {
        return Ok(json!({"supported":false,"authorization":"unsupported","devices":[]}));
    }
    assets.verify_camera()?;
    let mut child = crate::process::command(assets.camera())
        .arg("--list")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()?;
    let output = child.stdout.take().unwrap();
    let errors = child.stderr.take().unwrap();
    let reader = thread::spawn(move || -> std::io::Result<Vec<u8>> {
        let mut bytes = Vec::new();
        output.take(65537).read_to_end(&mut bytes)?;
        Ok(bytes)
    });
    let error_text = Arc::new(Mutex::new(String::new()));
    let error_copy = Arc::clone(&error_text);
    let error_reader = thread::spawn(move || drain_errors(errors, &error_copy));
    let deadline = Instant::now() + Duration::from_secs(5);
    let status = loop {
        if let Some(status) = child.try_wait()? {
            break status;
        }
        if Instant::now() >= deadline {
            let _ = child.kill();
            let _ = child.wait();
            let _ = reader.join();
            let _ = error_reader.join();
            bail!("Host camera enumeration timed out");
        }
        thread::sleep(Duration::from_millis(5));
    };
    let bytes = reader
        .join()
        .map_err(|_| anyhow::anyhow!("Host camera enumeration failed"))??;
    let _ = error_reader.join();
    ensure!(
        status.success(),
        "Host camera enumeration failed with {status}: {}",
        error_text.lock().unwrap().trim()
    );
    ensure!(
        bytes.len() <= 65536,
        "Host camera enumeration response too large"
    );
    let value: Value = serde_json::from_slice(&bytes)?;
    ensure!(
        value["supported"] == true && value["devices"].is_array(),
        "Invalid host camera enumeration response"
    );
    Ok(value)
}

pub struct Frame {
    pub rgb: Vec<u8>,
    pub host_ns: u64,
    pub producer_dropped: u32,
}
#[derive(Default)]
struct Mailbox {
    latest: Option<Frame>,
    frames: u64,
    dropped: u64,
    last_host_ns: u64,
    producer_dropped: u32,
    ended_at: Option<Instant>,
    error: Option<String>,
}
impl Mailbox {
    fn put(&mut self, frame: Frame) {
        self.frames += 1;
        self.last_host_ns = frame.host_ns;
        self.producer_dropped = frame.producer_dropped;
        if self.latest.replace(frame).is_some() {
            self.dropped += 1;
        }
    }
}

/// Validate the entire record before allocating; partial records never publish.
fn read_frame(reader: &mut impl Read, rotation: u16) -> Result<Frame> {
    let mut header = [0; 32];
    reader
        .read_exact(&mut header)
        .context("Host camera stream ended or has an incomplete header")?;
    ensure!(
        &header[..8] == b"LCAMRGB1",
        "Invalid host camera frame magic"
    );
    let field = |offset| u32::from_le_bytes(header[offset..offset + 4].try_into().unwrap());
    let (width, height, length, producer_dropped) = (field(8), field(12), field(16), field(20));
    ensure!(
        width > 0
            && width <= MAX_WIDTH
            && height > 0
            && height <= MAX_HEIGHT
            && length == width * height * 3,
        "Invalid host camera frame geometry"
    );
    let mut rgb = vec![0; length as usize];
    reader
        .read_exact(&mut rgb)
        .context("Host camera frame is incomplete")?;
    let source =
        image::RgbImage::from_raw(width, height, rgb).context("Invalid host camera RGB frame")?;
    Ok(Frame {
        rgb: camera::adapt(&source, rotation)?,
        host_ns: u64::from_le_bytes(header[24..32].try_into().unwrap()),
        producer_dropped,
    })
}
fn drain_errors(mut reader: impl Read, output: &Arc<Mutex<String>>) {
    let mut buffer = [0; 1024];
    while let Ok(length) = reader.read(&mut buffer) {
        if length == 0 {
            break;
        }
        let mut text = output.lock().unwrap();
        text.push_str(&String::from_utf8_lossy(&buffer[..length]));
        while text.len() > MAX_ERROR {
            let boundary = text
                .char_indices()
                .find(|(index, _)| *index >= text.len() - MAX_ERROR)
                .unwrap()
                .0;
            text.drain(..boundary);
        }
    }
}

pub struct Capture {
    pub device_id: String,
    child: Option<Child>,
    reader: Option<JoinHandle<()>>,
    errors_reader: Option<JoinHandle<()>>,
    mailbox: Arc<Mutex<Mailbox>>,
    errors: Arc<Mutex<String>>,
    errors_done: Arc<AtomicBool>,
    pub started: Instant,
    pub disconnected_published: bool,
    publication_drops: u64,
}
impl Capture {
    pub fn start(assets: &Assets, device_id: &str, rotation: u16) -> Result<Self> {
        ensure!(
            cfg!(target_os = "macos"),
            "Host camera capture is unsupported on this platform"
        );
        ensure!(
            !device_id.is_empty() && device_id.len() <= 4096,
            "Invalid host camera device ID"
        );
        assets.verify_camera()?;
        let mut command = crate::process::command(assets.camera());
        command.args(["--device", device_id]);
        Self::spawn(command, device_id, rotation)
    }
    fn spawn(mut command: std::process::Command, device_id: &str, rotation: u16) -> Result<Self> {
        let mut child = command
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .context("Cannot start host camera capture")?;
        let mut output = child.stdout.take().unwrap();
        let errors = Arc::new(Mutex::new(String::new()));
        let stderr = child.stderr.take().unwrap();
        let errors_copy = Arc::clone(&errors);
        let errors_done = Arc::new(AtomicBool::new(false));
        let done = Arc::clone(&errors_done);
        let errors_reader = thread::spawn(move || {
            drain_errors(stderr, &errors_copy);
            done.store(true, Ordering::Release);
        });
        let mailbox = Arc::new(Mutex::new(Mailbox::default()));
        let latest = Arc::clone(&mailbox);
        let reader = thread::spawn(move || {
            loop {
                match read_frame(&mut output, rotation) {
                    Ok(frame) => latest.lock().unwrap().put(frame),
                    Err(error) => {
                        let mut mailbox = latest.lock().unwrap();
                        mailbox.ended_at = Some(Instant::now());
                        mailbox.error = Some(error.to_string());
                        break;
                    }
                }
            }
        });
        Ok(Self {
            device_id: device_id.into(),
            child: Some(child),
            reader: Some(reader),
            errors_reader: Some(errors_reader),
            mailbox,
            errors,
            errors_done,
            started: Instant::now(),
            disconnected_published: false,
            publication_drops: 0,
        })
    }
    pub fn take(&mut self) -> Option<Frame> {
        let terminal = self.ended();
        let mut mailbox = self.mailbox.lock().unwrap();
        if mailbox.ended_at.is_some() {
            // EOF on stdout can precede the native error on stderr. Give its
            // reader a bounded grace period before terminating a malformed
            // producer, without waiting on the runtime thread.
            if terminal && let Some(child) = &mut self.child {
                let _ = child.kill();
            }
            mailbox.latest = None;
            return None;
        }
        mailbox.latest.take()
    }
    pub fn ended(&self) -> bool {
        self.mailbox.lock().unwrap().ended_at.is_some_and(|at| {
            self.errors_done.load(Ordering::Acquire) || at.elapsed() >= ERROR_DRAIN_GRACE
        })
    }
    pub fn dropped(&mut self) {
        self.publication_drops += 1;
    }
    pub fn error(&self) -> Option<String> {
        let errors = self.errors.lock().unwrap();
        if !errors.trim().is_empty() {
            return Some(errors.trim().into());
        }
        self.mailbox.lock().unwrap().error.clone()
    }
    pub fn status(&self, pending: bool) -> Value {
        let terminal = self.ended();
        let mailbox = self.mailbox.lock().unwrap();
        let state = if terminal {
            if mailbox.frames == 0 {
                "error"
            } else {
                "disconnected"
            }
        } else if pending {
            "starting"
        } else {
            "live"
        };
        let value = json!({"state":state,"device_id":self.device_id,"frames":mailbox.frames,"dropped":u64::from(mailbox.producer_dropped)+mailbox.dropped+self.publication_drops,"producer_dropped":mailbox.producer_dropped,"mailbox_dropped":mailbox.dropped,"publication_dropped":self.publication_drops,"last_frame_host_ns":mailbox.last_host_ns});
        drop(mailbox);
        let mut value = value;
        value["error"] = if terminal {
            json!(self.error())
        } else {
            Value::Null
        };
        value
    }
}
impl Drop for Capture {
    fn drop(&mut self) {
        // Kill immediately; joins and process reaping cannot hold up the control
        // thread (the reader may currently be adapting its last frame).
        let mut child = self.child.take().unwrap();
        let _ = child.kill();
        let reader = self.reader.take();
        let errors = self.errors_reader.take();
        thread::spawn(move || {
            let _ = child.wait();
            if let Some(reader) = reader {
                let _ = reader.join();
            }
            if let Some(errors) = errors {
                let _ = errors.join();
            }
        });
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn record(width: u32, height: u32) -> Vec<u8> {
        let mut result = b"LCAMRGB1".to_vec();
        for value in [width, height, width * height * 3, 0] {
            result.extend(value.to_le_bytes());
        }
        result.extend(123_u64.to_le_bytes());
        result.extend(vec![70; (width * height * 3) as usize]);
        result
    }
    #[test]
    fn records_are_bounded_and_complete() {
        let bytes = record(4, 2);
        let frame = read_frame(&mut &bytes[..], 90).unwrap();
        assert_eq!(frame.host_ns, 123);
        assert_eq!(frame.rgb.len(), 640 * 480 * 3);
        for bytes in [&bytes[..31], &bytes[..bytes.len() - 1]] {
            assert!(read_frame(&mut &bytes[..], 0).is_err());
        }
        for (width, height) in [(0, 1), (1921, 1), (1, 1081)] {
            assert!(read_frame(&mut &record(width, height)[..], 0).is_err());
        }
        let mut bad = record(1, 1);
        bad[20..24].copy_from_slice(&u32::MAX.to_le_bytes());
        assert_eq!(
            read_frame(&mut &bad[..], 0).unwrap().producer_dropped,
            u32::MAX
        );
        bad[16] = 2;
        assert!(read_frame(&mut &bad[..], 0).is_err());
        bad[16] = 3;
        bad[0] = 0;
        assert!(read_frame(&mut &bad[..], 0).is_err());
    }
    #[test]
    fn mailbox_keeps_only_latest_and_counts_drops() {
        let mut mailbox = Mailbox::default();
        for host_ns in 1..100 {
            mailbox.put(Frame {
                rgb: vec![0; 3],
                host_ns,
                producer_dropped: 17,
            });
        }
        assert_eq!(mailbox.frames, 99);
        assert_eq!(mailbox.dropped, 98);
        assert_eq!(mailbox.producer_dropped, 17);
        assert_eq!(mailbox.latest.take().unwrap().host_ns, 99);
    }
    #[test]
    fn live_adapter_matches_still_mounting() {
        let source =
            image::RgbImage::from_fn(4, 2, |x, y| image::Rgb([x as u8 * 40, y as u8 * 70, 0]));
        let mut bytes = record(4, 2);
        bytes[32..].copy_from_slice(source.as_raw());
        for rotation in [0, 90, 180, 270] {
            assert_eq!(
                read_frame(&mut &bytes[..], rotation).unwrap().rgb,
                camera::adapt(&source, rotation).unwrap()
            );
        }
    }
    #[cfg(unix)]
    #[test]
    fn denied_and_missing_devices_surface_bounded_errors() {
        for error in ["Camera permission denied", "Camera device was disconnected"] {
            let mut command = std::process::Command::new("/bin/sh");
            command.args(["-c", "printf '%s' \"$1\" >&2; exit 1", "test", error]);
            let capture = Capture::spawn(command, "missing", 0).unwrap();
            let deadline = Instant::now() + Duration::from_secs(2);
            while !capture.ended() && Instant::now() < deadline {
                thread::sleep(Duration::from_millis(1));
            }
            assert!(capture.ended());
            assert_eq!(capture.status(true)["state"], "error");
            assert_eq!(capture.error().as_deref(), Some(error));
        }
        let errors = Arc::new(Mutex::new(String::new()));
        drain_errors(&vec![b'x'; MAX_ERROR * 4][..], &errors);
        assert_eq!(errors.lock().unwrap().len(), MAX_ERROR);
    }
    #[cfg(unix)]
    #[test]
    fn stdout_eof_preserves_native_error_after_stderr_drains() {
        let mut command = std::process::Command::new("/bin/sh");
        command.args([
            "-c",
            "exec 1>&-; sleep 0.03; printf 'Camera permission denied' >&2; exit 1",
        ]);
        let mut capture = Capture::spawn(command, "test", 0).unwrap();
        let deadline = Instant::now() + Duration::from_secs(2);
        // The grace period can expire before stderr is scheduled on a busy
        // host. Wait for both readers before checking the final diagnostic.
        // The held-open-stderr test below checks bounded, nonblocking polling.
        while !(capture.ended() && capture.errors_done.load(Ordering::Acquire))
            && Instant::now() < deadline
        {
            thread::sleep(Duration::from_millis(1));
        }
        assert!(capture.ended());
        assert!(capture.errors_done.load(Ordering::Acquire));
        assert!(capture.take().is_none());
        assert_eq!(capture.status(true)["error"], "Camera permission denied");
    }
    #[cfg(unix)]
    #[test]
    fn error_drain_grace_is_bounded_when_stderr_stays_open() {
        let mut command = std::process::Command::new("/bin/sh");
        command.args(["-c", "exec 1>&-; exec sleep 30"]);
        let mut capture = Capture::spawn(command, "test", 0).unwrap();
        let start = Instant::now();
        while !capture.ended() && start.elapsed() < Duration::from_secs(2) {
            assert!(capture.take().is_none());
            thread::sleep(Duration::from_millis(1));
        }
        assert!(capture.ended());
        assert!(start.elapsed() >= ERROR_DRAIN_GRACE);
        assert!(start.elapsed() < Duration::from_secs(1));
        assert!(capture.take().is_none());
    }
    #[cfg(unix)]
    #[test]
    fn stopping_capture_does_not_wait_for_first_frame() {
        let mut command = std::process::Command::new("/bin/sh");
        command.args(["-c", "exec sleep 30"]);
        let capture = Capture::spawn(command, "test", 0).unwrap();
        let start = Instant::now();
        drop(capture);
        assert!(start.elapsed() < Duration::from_millis(100));
    }
}
