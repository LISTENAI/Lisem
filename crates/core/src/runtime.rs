//! One instance's runtime. QEMU owns guest execution; this module owns its
//! storage lease, raw transport, host adapters and bounded lifecycle.
use crate::{
    assets::{Assets, Backend},
    catalog::Catalog,
    display::{Display, Frame},
    shared::Regions,
    storage::{self, Lease},
    transport::{self, Qmp, SerialPort, Uart},
};
use anyhow::{Context, Result, bail, ensure};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    fs::{self, File},
    io::{BufRead, BufReader, Read, Write},
    net::TcpStream,
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
    thread,
    time::{Duration, Instant},
};
use uuid::Uuid;

fn qemu_failure(errors: &str) -> String {
    let failure: String = errors
        .lines()
        .rev()
        .find(|line| line.contains("ARCS"))
        .unwrap_or("QEMU failed")
        .chars()
        .take(2048)
        .collect();
    // TX emits a fixed model reason before the generic MMIO location.
    // Preserve it without forwarding arbitrary stderr or guest data.
    let tx_reason = errors.lines().rev().find_map(|line| {
        ["ARCS Wi-Fi AC1: ", "ARCS Wi-Fi AC3: "]
            .iter()
            .find_map(|prefix| line.find(prefix).map(|at| &line[at..]))
    });
    match tx_reason {
        Some(reason) if !failure.contains(reason) => {
            let reason: String = reason.chars().take(512).collect();
            format!("{reason}; {failure}")
        }
        _ => failure,
    }
}

#[derive(Clone, Serialize, Deserialize)]
pub struct Options {
    pub seconds: u64,
    pub timeout: u64,
    pub network: bool,
    pub host_audio: bool,
    pub microphone: bool,
    pub sound: bool,
    pub download: bool,
    #[serde(default)]
    pub capture: Option<PathBuf>,
}
impl Default for Options {
    fn default() -> Self {
        Self {
            seconds: 300,
            timeout: 800,
            network: false,
            host_audio: false,
            microphone: false,
            sound: true,
            download: false,
            capture: None,
        }
    }
}

/// A bounded sequence is scheduled entirely by the guest virtual clock.
#[derive(Deserialize)]
struct ButtonSequence {
    button: String,
    count: u64,
    hold_ms: u64,
    gap_ms: u64,
}
impl ButtonSequence {
    fn command(&self) -> Result<String> {
        ensure!((1..=32).contains(&self.count), "Button count must be 1..32");
        ensure!(
            (1..=60000).contains(&self.hold_ms),
            "Hold must be 1..60000 ms"
        );
        ensure!(
            self.gap_ms <= 60000 && (self.count == 1 || self.gap_ms > 0),
            "Repeated presses require a positive gap of at most 60000 ms"
        );
        ensure!(
            self.count * self.hold_ms + (self.count - 1) * self.gap_ms <= 60000,
            "Button sequence must fit within 60000 ms"
        );
        let prefix = match self.button.as_str() {
            "function" => None,
            "k1" => Some(0),
            "k2" => Some(1),
            "k3" => Some(2),
            _ => bail!("Unknown board button"),
        };
        Ok(match prefix {
            Some(id) => format!("{},{},{},{}", id, self.count, self.hold_ms, self.gap_ms),
            None => format!("{},{},{}", self.count, self.hold_ms, self.gap_ms),
        })
    }
}

struct Process {
    child: Child,
}
impl Process {
    fn guest(command: &mut Command, lease: &Lease) -> Result<Self> {
        #[cfg(unix)]
        {
            use std::os::{fd::AsRawFd, unix::process::CommandExt};
            let fd = lease.file.as_raw_fd();
            // Inherit the same open-file description: a crashed controller
            // cannot release storage while its QEMU child still writes Flash.
            unsafe {
                command.pre_exec(move || {
                    if libc::fcntl(fd, libc::F_SETFD, 0) < 0 {
                        return Err(std::io::Error::last_os_error());
                    }
                    Ok(())
                });
            }
            Ok(Self {
                child: command.spawn()?,
            })
        }
        #[cfg(windows)]
        {
            // QEMU has no console input: its monitor and UARTs use sockets.
            // The inherited standard handle retains the share-denying lease
            // even if the controller exits before its child.
            command.stdin(Stdio::from(lease.file.try_clone()?));
            Ok(Self {
                child: command.spawn()?,
            })
        }
    }
}
impl Drop for Process {
    fn drop(&mut self) {
        if self.child.try_wait().ok().flatten().is_none() {
            let _ = self.child.kill();
            let _ = self.child.wait();
        }
    }
}
/// Drain diagnostic pipes independently, retaining only a bounded tail in RAM.
struct PipeLog {
    bytes: Arc<Mutex<Vec<u8>>>,
    reader: Option<thread::JoinHandle<()>>,
}
impl PipeLog {
    fn collect(mut reader: impl Read, bytes: &Mutex<Vec<u8>>) {
        let mut chunk = [0; 8192];
        while let Ok(count) = reader.read(&mut chunk) {
            if count == 0 {
                break;
            }
            let mut tail = bytes.lock().unwrap();
            tail.extend_from_slice(&chunk[..count]);
            let excess = tail.len().saturating_sub(256 * 1024);
            tail.drain(..excess);
        }
    }
    fn start(reader: impl Read + Send + 'static) -> Self {
        let bytes = Arc::new(Mutex::new(Vec::new()));
        let tail = bytes.clone();
        let reader = thread::spawn(move || Self::collect(reader, &tail));
        Self {
            bytes,
            reader: Some(reader),
        }
    }
    fn finish(&mut self) -> String {
        if let Some(reader) = self.reader.take() {
            let _ = reader.join();
        }
        String::from_utf8_lossy(&self.bytes.lock().unwrap()).into_owned()
    }
}

struct Audio {
    replies: std::sync::mpsc::Receiver<Value>,
    pending_microphone: Option<(bool, Instant)>,
    process: Process,
    stdout: PipeLog,
    stderr: PipeLog,
}
impl Audio {
    fn start(
        assets: &Assets,
        output: &Path,
        options: &Options,
        shared: Option<&str>,
    ) -> Result<Self> {
        assets.verify_audio()?;
        let directory = output.join("host-audio");
        if shared.is_none() {
            fs::create_dir(&directory)?;
        }
        let mut command = crate::process::command(assets.audio());
        command
            .arg(shared.map(Path::new).unwrap_or(&directory))
            .arg((options.timeout + 50).to_string())
            .arg(if options.microphone {
                "capture"
            } else {
                "playback"
            })
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        let mut child = command.spawn()?;
        let stdout = child.stdout.take().context("Audio stdout missing")?;
        let stderr = PipeLog::start(child.stderr.take().context("Audio stderr missing")?);
        let (tx, rx) = std::sync::mpsc::channel();
        let (reply_tx, replies) = std::sync::mpsc::sync_channel(4);
        let bytes = Arc::new(Mutex::new(Vec::new()));
        let tail = bytes.clone();
        let reader = thread::spawn(move || {
            let mut reader = BufReader::new(stdout);
            let mut line = String::new();
            let result = reader.read_line(&mut line).map(|_| line);
            let _ = tx.send(result);
            for line in reader.lines().map_while(std::result::Result::ok) {
                if let Ok(value) = serde_json::from_str::<Value>(&line)
                    && value.get("microphone").is_some()
                {
                    let _ = reply_tx.try_send(value);
                } else {
                    let mut bytes = tail.lock().unwrap();
                    bytes.extend_from_slice(line.as_bytes());
                    bytes.push(b'\n');
                    let excess = bytes.len().saturating_sub(256 * 1024);
                    bytes.drain(..excess);
                }
            }
        });
        let mut audio = Self {
            replies,
            pending_microphone: None,
            process: Process { child },
            stdout: PipeLog {
                bytes,
                reader: Some(reader),
            },
            stderr,
        };
        let line = rx
            .recv_timeout(Duration::from_secs(40))
            .context("Host audio initialization timed out")??;
        ensure!(
            !line.is_empty(),
            "Host audio initialization failed: {}",
            String::from_utf8_lossy(&audio.stderr.bytes.lock().unwrap()).trim()
        );
        let ready: Value =
            serde_json::from_str(&line).context("Invalid host audio initialization response")?;
        ensure!(
            ready == json!({"ready":true,"rate":16000,"channels":1}),
            "Host audio initialization failed"
        );
        audio.mute(!options.sound)?;
        Ok(audio)
    }
    fn microphone(&mut self, enabled: bool) -> Result<()> {
        ensure!(
            self.pending_microphone.is_none(),
            "Microphone change is still pending"
        );
        let input = self
            .process
            .child
            .stdin
            .as_mut()
            .context("Audio control closed")?;
        input.write_all(if enabled { b"c" } else { b"d" })?;
        input.flush()?;
        self.pending_microphone = Some((enabled, Instant::now()));
        Ok(())
    }
    fn mute(&mut self, muted: bool) -> Result<()> {
        let input = self
            .process
            .child
            .stdin
            .as_mut()
            .context("Audio control closed")?;
        input.write_all(if muted { b"1" } else { b"0" })?;
        input.flush()?;
        Ok(())
    }
    fn finish(&mut self, drain: bool) -> Result<()> {
        if !drain && let Some(input) = self.process.child.stdin.as_mut() {
            let _ = input.write_all(b"q");
            let _ = input.flush();
        }
        let deadline = Instant::now() + Duration::from_secs(if drain { 20 } else { 2 });
        loop {
            if let Some(status) = self.process.child.try_wait()? {
                let errors = self.stderr.finish();
                self.stdout.finish();
                ensure!(
                    status.success(),
                    "Host audio exited with {status}: {}",
                    errors.trim()
                );
                return Ok(());
            }
            ensure!(Instant::now() < deadline, "Host audio shutdown timed out");
            thread::sleep(Duration::from_millis(10));
        }
    }
}

pub struct Session {
    process: Process,
    qmp: Qmp,
    camera: Option<crate::camera_input::Input>,
    camera_capture: Option<crate::camera_capture::Capture>,
    camera_candidate: Option<crate::camera_capture::Capture>,
    camera_disconnecting: bool,
    uart: Vec<Uart>,
    audio: Option<Audio>,
    stdout: Option<PipeLog>,
    stderr: Option<PipeLog>,
    display: Display,
    frame: Option<Frame>,
    framebuffer: PathBuf,
    _regions: Regions,
    _workspace: Option<tempfile::TempDir>,
    _lease: Lease,
    pub output: PathBuf,
    pub state: Value,
    options: Options,
    started: Instant,
    capture_at: Instant,
    finished: bool,
    stopped: bool,
    backend: Backend,
    pending_releases: BTreeMap<String, String>,
    pending_cancel: bool,
}
impl Session {
    fn start(catalog: &Catalog, item: &Value, options: Options) -> Result<Self> {
        ensure!(
            (5..=86400).contains(&options.seconds) && (1..=172800).contains(&options.timeout),
            "Runtime budgets must be 5..86400 virtual seconds and 1..172800 host seconds"
        );
        ensure!(
            !options.microphone || options.host_audio,
            "Microphone requires host audio"
        );
        let camera_frame = item["host"]["camera_image"]
            .as_str()
            .map(|path| {
                let rotation = crate::camera::mounting_rotation(&item["hardware"]["board"])?;
                crate::camera::load(Path::new(path), rotation)
            })
            .transpose()?;
        let assets = Assets::new(&catalog.root)?;
        assets.verify_qemu()?;
        // Snapshot launch provenance; later updates on disk must not relabel
        // an already running QEMU process.
        let qemu_sha256 = storage::sha256(&assets.qemu())?;
        let backend = Backend::for_device(catalog, item)?;
        let instance = Path::new(item["path"].as_str().context("Missing instance path")?);
        let lease = storage::locked(
            instance,
            &catalog.layout(item["board"].as_str().context("Missing board")?)?,
        )?;
        let mut regions = Regions::new(instance)?;
        let workspace = if options.capture.is_none() {
            Some(tempfile::tempdir()?)
        } else {
            None
        };
        let output = if let Some(path) = &options.capture {
            let path = std::path::absolute(path)?;
            fs::create_dir_all(&path)?;
            let output = path.join(format!("capture-{}", Uuid::new_v4().simple()));
            fs::create_dir(&output)?;
            output
        } else {
            workspace.as_ref().unwrap().path().to_owned()
        };
        let framebuffer = if options.capture.is_some() {
            output.join("live/framebuffer")
        } else {
            PathBuf::from(regions.allocate()?)
        };
        let camera_shared = item["hardware"]["board"]["camera"]
            .is_object()
            .then(|| regions.allocate())
            .transpose()?;
        fs::create_dir_all(output.join("live"))?;
        let mut command = crate::process::command(assets.qemu());
        backend.configure(
            &mut command,
            instance,
            &output,
            options.seconds,
            options.download,
            options.capture.is_some(),
        );
        command.env(
            "ARCS_QEMU_WIFI_ENVIRONMENT",
            serde_json::to_string(&crate::wifi_environment::WifiEnvironment::from_value(
                &item["environment"]["wifi"],
            )?)?,
        );
        command.stdin(Stdio::null());
        command.env(
            "ARCS_QEMU_WIFI_AP_PRESENT",
            if item["host"]["wifi_ap_present"] == false {
                "0"
            } else {
                "1"
            },
        );
        if let Some(name) = &camera_shared {
            command.env("ARCS_QEMU_CAMERA_SHM", name);
        }
        if options.capture.is_some() {
            command
                .stdout(File::create(output.join("qemu.log"))?)
                .stderr(File::options().append(true).open(output.join("qemu.log"))?);
        } else {
            command
                .stdout(Stdio::piped())
                .stderr(Stdio::piped())
                .env("ARCS_QEMU_DISPLAY_SHM", &framebuffer);
        }
        if options.network {
            ensure!(
                assets.network().is_file(),
                "Host network backend is missing"
            );
            command
                .env("ARCS_QEMU_NETWORK_LIBRARY", assets.network())
                .env("ARCS_QEMU_NETWORK_LOOPBACK", "0");
            if options.capture.is_some() {
                command.env(
                    "ARCS_QEMU_NETWORK_CAPTURE",
                    output.join("host-network.pcap"),
                );
            }
        }
        let audio_shared = if options.host_audio && options.capture.is_none() {
            Some(regions.allocate()?)
        } else {
            None
        };
        let audio = if options.host_audio {
            Some(Audio::start(
                &assets,
                &output,
                &options,
                audio_shared.as_deref(),
            )?)
        } else {
            None
        };
        if audio.is_some() {
            command.env(
                "ARCS_QEMU_HOST_AUDIO",
                audio_shared
                    .as_deref()
                    .map(Path::new)
                    .map(Path::to_owned)
                    .unwrap_or_else(|| output.join("host-audio/stream.bin")),
            );
        }
        let qmp_listener = transport::listener()?;
        command.args([
            "-chardev",
            &format!(
                "socket,id=control,host=127.0.0.1,port={},nodelay=on",
                qmp_listener.local_addr()?.port()
            ),
            "-mon",
            "chardev=control,mode=control",
        ]);
        let listeners: Vec<_> = (0..3)
            .map(|_| transport::listener())
            .collect::<Result<_>>()?;
        for (channel, listener) in listeners.iter().enumerate() {
            command.args([
                "-chardev",
                &format!(
                    "socket,id=uart{channel},host=127.0.0.1,port={},nodelay=on",
                    listener.local_addr()?.port()
                ),
                "-serial",
                &format!("chardev:uart{channel}"),
            ]);
        }
        let mut process = Process::guest(&mut command, &lease)?;
        let stdout = process.child.stdout.take().map(PipeLog::start);
        let stderr = process.child.stderr.take().map(PipeLog::start);
        let control = transport::accept(&qmp_listener)?;
        let mut uart = Vec::new();
        for (channel, listener) in listeners.iter().enumerate() {
            let path = output.join(format!("uart{channel}.bin"));
            uart.push(Uart::new(
                transport::accept(listener)?,
                options.capture.as_ref().map(|_| path.as_path()),
            )?);
            if options.capture.is_some() {
                fs::hard_link(
                    &path,
                    output.join("live").join(format!("uart{channel}.bin")),
                )?;
            }
        }
        let mut qmp = Qmp::new(control, &mut uart)?;
        let qemu_version = qmp.call("query-version", json!({}), &mut uart)?;
        let qemu_identity = json!({"version":qemu_version,"sha256":qemu_sha256,
            "pid":process.child.id()});
        let display = Display::open(&framebuffer)?;
        let camera = camera_shared
            .as_deref()
            .map(crate::camera_input::Input::open)
            .transpose()?;
        if options.capture.is_some() {
            storage::write_json(
                &output.join("run.json"),
                &json!({"qemu_sha256":storage::sha256(&assets.qemu())?,"flash_sha256":storage::sha256(&instance.join("flash.bin"))?,"otp_sha256":storage::sha256(&instance.join("otp.bin"))?,"boot_source":"chip-rom","options":options}),
            )?;
        }
        let mut session = Self {
            process,
            pending_releases: BTreeMap::new(),
            pending_cancel: false,
            qmp,
            camera,
            camera_capture: None,
            camera_candidate: None,
            camera_disconnecting: false,
            uart,
            audio,
            stdout,
            stderr,
            display,
            frame: None,
            framebuffer,
            _regions: regions,
            _workspace: workspace,
            _lease: lease,
            output,
            state: json!({"qemu":qemu_identity}),
            options,
            started: Instant::now(),
            capture_at: Instant::now(),
            finished: false,
            stopped: false,
            backend,
        };
        if let Some(frame) = camera_frame {
            let generation = session
                .camera
                .as_mut()
                .context("Camera input map is missing")?
                .prepare(crate::camera_input::SourceState::Still, Some(&frame), 0)?;
            session.qmp.set(
                "x-lisa-camera-source",
                json!(generation.to_string()),
                &mut session.uart,
            )?;
            session
                .camera
                .as_mut()
                .unwrap()
                .confirmed(generation, true)?;
        }
        session.state["camera_image"] = item["host"]["camera_image"].clone();
        session.state["camera_device"] = item["host"]["camera_device"].clone();
        session.state["camera_capture"] = json!({"state":"off","device_id":null,"frames":0,"dropped":0,"last_frame_host_ns":0,"error":null});
        if let Some(device) = item["host"]["camera_device"].as_str() {
            let rotation = crate::camera::mounting_rotation(&item["hardware"]["board"])?;
            match crate::camera_capture::Capture::start(&assets, device, rotation) {
                Ok(capture) => {
                    session.state["camera_capture"] = capture.status(true);
                    session.state["camera_change"] = json!({"status":"pending","phase":"waiting-first-frame","device_id":device});
                    session.camera_candidate = Some(capture);
                }
                Err(error) => {
                    session.state["camera_capture"] = json!({"state":"error","device_id":device,"frames":0,"dropped":0,"last_frame_host_ns":0,"error":error.to_string()});
                    session.state["camera_change"] =
                        json!({"status":"rejected","device_id":device,"error":error.to_string()});
                }
            }
        }
        session.capture()?;
        session.qmp.call("cont", json!({}), &mut session.uart)?;
        Ok(session)
    }
    fn capture(&mut self) -> Result<()> {
        let snapshot = self.qmp.get("x-lisa-snapshot", &mut self.uart)?;
        let fields: Value =
            serde_json::from_str(snapshot.as_str().context("Invalid QEMU snapshot")?)?;
        for (key, value) in fields.as_object().context("Invalid snapshot object")? {
            self.state[key] = value.clone();
        }
        self.state["continuous_audio"] = json!(self.audio.is_some());
        self.state["microphone"] = json!(self.options.microphone);
        self.state["elapsed_seconds"] = json!(self.started.elapsed().as_secs_f64());
        self.state["budget"] = json!({"virtual_seconds":self.options.seconds,
            "host_seconds":self.options.timeout,
            "virtual_remaining_seconds":(self.options.seconds as f64 - self.state["seconds"].as_f64().unwrap_or(0.0)).max(0.0),
            "host_remaining_seconds":(self.options.timeout as f64 - self.started.elapsed().as_secs_f64()).max(0.0)});
        self.state["output"] = json!(self.output);
        self.state["framebuffer"] = json!(self.framebuffer);
        if self.options.capture.is_some() {
            storage::write_json(&self.output.join("live/state.json"), &self.state)?;
        }
        self.capture_at = Instant::now() + Duration::from_millis(100);
        Ok(())
    }
    fn button_sequence(&mut self) -> Result<Value> {
        let value = self.qmp.get("x-lisa-button-sequence", &mut self.uart)?;
        let state: Value =
            serde_json::from_str(value.as_str().context("Invalid button sequence state")?)?;
        self.state["button_sequence"] = state.clone();
        let button = match state["button_id"].as_u64() {
            Some(0) => Some("k1"),
            Some(1) => Some("k2"),
            Some(2) => Some("k3"),
            _ => Some("function"),
        };
        if let Some(button) = button {
            self.state["controls"]["buttons"][button] = state["pressed"].clone();
        }
        Ok(state)
    }
    fn tick(&mut self) -> Result<()> {
        if self.finished {
            return Ok(());
        }
        for port in &mut self.uart {
            port.pump()?;
        }
        if let Some(status) = self.process.child.try_wait()? {
            self.end_camera_capture();
            if let Some(audio) = self.audio.as_mut()
                && let Some((enabled, _)) = audio.pending_microphone.take()
            {
                self.state["microphone_change"] = json!({"status":"cancelled","enabled":enabled});
            }
            self.state["microphone"] = json!(false);
            self.finished = true;
            self.state["finished"] = json!(true);
            self.state["returncode"] = json!(status.code().unwrap_or(-1));
            let errors = self
                .stderr
                .as_mut()
                .map(PipeLog::finish)
                .unwrap_or_else(|| {
                    fs::read_to_string(self.output.join("qemu.log")).unwrap_or_default()
                });
            let report = if let Some(stdout) = &mut self.stdout {
                stdout
                    .finish()
                    .lines()
                    .rev()
                    .find_map(|line| serde_json::from_str::<Value>(line).ok())
            } else {
                storage::read_json(&self.output.join("report.json")).ok()
            };
            if let Some(report) = report {
                if let Some(input) = report.get("audio_input") { self.state["audio_input"] = input.clone(); }
                if let Some(sequence) = report.get("button_sequence") {
                    self.state["button_sequence"] = sequence.clone();
                }
                self.state["report"] = report.clone();
                self.state["seconds"] = json!(report["virtual_ns"].as_f64().unwrap_or(0.0) / 1e9);
                self.state["ap_exceptions"] = report["cores"][0]["exceptions"].clone();
                self.state["exceptions"] = report["cores"][1]["exceptions"].clone();
                self.state["machine_status"] = report["status"].clone();
                if ![Some("budget-complete"), Some("probe-pass")]
                    .contains(&report["status"].as_str())
                {
                    self.state["error"] = json!(qemu_failure(&errors));
                }
            } else if !self.stopped && self.state["error"].is_null() {
                self.state["error"] = json!("QEMU exited without a readable report");
            }
            if !status.success() && !self.stopped && self.state["error"].is_null() {
                self.state["error"] = json!(format!("QEMU exited with {status}"));
            }
            if self.state["audio_input"]["status"] == "running" {
                self.state["audio_input"]["status"] = json!("interrupted");
            }
            self.state["input_busy"] = json!(false);
            if self.state["button_sequence"]["status"] == "running" {
                // Without a final report, completion cannot be inferred.
                self.state["button_sequence"]["status"] = json!("interrupted");
                self.state["button_sequence"]["reason"] = json!("runtime-ended");
            }
            if self.state["button_sequence"].is_object() {
                self.state["button_sequence"]["pressed"] = json!(false);
            }
            if self.backend.machine == "arcs-evb" {
                for button in ["k1", "k2", "k3"] {
                    self.state["controls"]["buttons"][button] = json!(false);
                }
            } else {
                self.state["controls"]["buttons"]["function"] = json!(false);
            }
            if let Some(audio) = &mut self.audio
                && let Err(error) = audio.finish(!self.stopped)
                && self.state["error"].is_null()
            {
                self.state["error"] = json!(error.to_string());
            }
            if let Some(audio) = &self.audio {
                if let Ok(report) =
                    serde_json::from_slice::<Value>(&audio.stdout.bytes.lock().unwrap())
                {
                    self.state["audio"] = report;
                }
            }
            if self.options.capture.is_some() {
                storage::write_json(&self.output.join("live/state.json"), &self.state)?;
            }
        } else if let Some(audio) = &mut self.audio
            && let Some(status) = audio.process.child.try_wait()?
        {
            // QEMU marks the stream complete before its process handle signals.
            // The helper can finish draining during that exit window.
            if status.success() {
                let deadline = Instant::now() + Duration::from_millis(100);
                while Instant::now() < deadline {
                    if self.process.child.try_wait()?.is_some() {
                        return self.tick();
                    }
                    thread::sleep(Duration::from_millis(2));
                }
            }
            self.state["error"] = json!(format!("Host audio exited unexpectedly with {status}"));
            self.stop()?;
        } else if self.started.elapsed().as_secs() >= self.options.timeout {
            self.state["error"] = json!("Host time budget reached");
            self.stop()?;
        } else if self.qmp.pending_id().is_none()
            && Instant::now() >= self.capture_at
            && let Err(error) = self.capture()
        {
            if error.is::<transport::ControlPending>() {
                return Ok(());
            }
            // QMP closes before the process handle becomes signalled on Windows.
            // Let normal exit collect the report and drain the last DAC frames.
            let deadline = Instant::now() + Duration::from_millis(100);
            loop {
                if self.process.child.try_wait()?.is_some() {
                    return self.tick();
                }
                if Instant::now() >= deadline {
                    break;
                }
                thread::sleep(Duration::from_millis(2));
            }
            return Err(error);
        }
        Ok(())
    }
    fn frame(&mut self) -> Option<Frame> {
        if let Some(frame) = self.display.latest() {
            self.frame = Some(frame);
        }
        self.frame.clone()
    }
    fn end_camera_capture(&mut self) {
        if self.camera_candidate.is_some() && self.state["camera_change"]["status"] == "pending" {
            self.state["camera_change"]["status"] = json!("rejected");
            self.state["camera_change"]["error"] =
                json!("Camera capture stopped before source selection was confirmed");
        }
        self.camera_candidate = None;
        self.camera_capture = None;
        self.state["camera_capture"]["state"] = json!("off");
    }
    fn disconnect_camera_input(&mut self) -> Result<()> {
        if self.camera_disconnecting {
            let input = self
                .camera
                .as_mut()
                .context("Camera input map is missing")?;
            if input.publish(None, crate::camera_input::SourceState::Disconnected, 0)? {
                self.camera_disconnecting = false;
            }
        }
        Ok(())
    }
    fn close_camera_input(&mut self) -> Result<()> {
        self.camera_disconnecting |= self.camera_capture.is_some();
        // Explicit close releases the physical device before awaiting QEMU.
        // A temporary unavailable publication retires the old live scene while
        // Clear's independent source generation waits for acknowledgement.
        self.end_camera_capture();
        self.disconnect_camera_input()
    }
    fn selected_camera(&mut self, source: &CameraSource, applied: bool) {
        if applied {
            self.camera_disconnecting = false;
        }
        if source.device.is_some() {
            if applied {
                self.camera_capture = self.camera_candidate.take();
                if let Some(capture) = &self.camera_capture {
                    self.state["camera_capture"] = capture.status(false);
                }
            } else {
                self.camera_candidate = None;
            }
        } else if applied {
            self.end_camera_capture();
        }
    }
    fn stop(&mut self) -> Result<()> {
        self.end_camera_capture();
        if self.finished {
            return Ok(());
        }
        self.stopped = true;
        let _ = self
            .qmp
            .set("x-lisa-button-sequence", json!("cancel"), &mut self.uart);
        let _ = self.button_sequence();
        let _ = self.qmp.call("quit", json!({}), &mut self.uart);
        let deadline = Instant::now() + Duration::from_secs(3);
        while self.process.child.try_wait()?.is_none() && Instant::now() < deadline {
            for port in &mut self.uart {
                let _ = port.pump();
            }
            thread::sleep(Duration::from_millis(2));
        }
        if self.process.child.try_wait()?.is_none() {
            self.process.child.kill()?;
            self.process.child.wait()?;
        }
        self.tick()
    }
}
impl Drop for Session {
    fn drop(&mut self) {
        let _ = self.stop();
    }
}

#[derive(Clone, Default)]
struct CameraSource {
    path: Option<PathBuf>,
    device: Option<String>,
}
impl CameraSource {
    fn from_params(params: &Value) -> Result<Self> {
        if let Some(device) = params.get("device_id") {
            ensure!(
                params.get("path").is_none(),
                "Camera image path and device ID are mutually exclusive"
            );
            let id = device
                .as_str()
                .filter(|id| !id.is_empty() && id.len() <= 4096)
                .context("Invalid host camera device ID")?;
            ensure!(
                cfg!(target_os = "macos"),
                "Host camera capture is unsupported on this platform"
            );
            return Ok(Self {
                path: None,
                device: Some(id.into()),
            });
        }
        let value = params
            .get("path")
            .context("Camera image path or device ID is required")?;
        ensure!(
            value.is_null() || value.is_string(),
            "Camera image path must be a string or null"
        );
        Ok(Self {
            path: value
                .as_str()
                .map(|p| std::path::absolute(Path::new(p)))
                .transpose()?,
            device: None,
        })
    }
    fn properties(&self) -> Value {
        json!({"path":self.path,"device_id":self.device})
    }
    fn settings(&self) -> Value {
        json!({"camera_image":self.path,"camera_device":self.device})
    }
}

fn finish_camera_change(
    catalog: &Catalog,
    id: &str,
    state: &mut Value,
    source: CameraSource,
    result: Result<Value>,
) {
    let mut change = source.properties();
    match result {
        Ok(_) => {
            state["camera_image"] = json!(source.path);
            state["camera_device"] = json!(source.device);
            let saved = catalog.device(id).and_then(|item| {
                catalog.update(
                    Path::new(item["path"].as_str().unwrap()),
                    &source.settings(),
                )
            });
            change["status"] = json!("applied");
            if let Err(error) = saved {
                change["error"] = json!(format!(
                    "Camera input applied but configuration was not saved: {error}"
                ));
            }
        }
        Err(error) => {
            change["status"] = json!("rejected");
            change["error"] = json!(error.to_string());
            if source.device.is_some() {
                state["camera_capture"]["state"] = json!("error");
                state["camera_capture"]["error"] = json!(error.to_string());
            }
        }
    }
    state["camera_change"] = change;
}

fn finish_access_point_change(
    catalog: &Catalog,
    id: &str,
    state: &mut Value,
    present: bool,
    result: Result<()>,
) {
    let mut change = json!({"status":"applied","enabled":present});
    match result {
        Ok(()) => {
            state["wifi_ap_present"] = json!(present);
            if let Err(error) = catalog.device(id).and_then(|item| {
                catalog
                    .update(
                        Path::new(item["path"].as_str().unwrap()),
                        &json!({"wifi_ap_present":present}),
                    )
                    .map(|_| ())
            }) {
                change["error"] = json!(format!(
                    "Access point changed but configuration was not saved: {error}"
                ));
            }
        }
        Err(error) => {
            change["status"] = json!("rejected");
            change["error"] = json!(error.to_string());
        }
    }
    state["access_point_change"] = change;
}

fn finish_wifi_environment_change(
    catalog: &Catalog, id: &str, state: &mut Value,
    request: &str, environment: Value, result: Result<()>,
) {
    let mut change = json!({"request":request,"status":"applied"});
    match result {
        Ok(()) => {
            if let Err(error) = catalog.device(id).and_then(|item| {
                catalog.update(Path::new(item["path"].as_str().unwrap()),
                               &json!({"wifi_environment":environment})).map(|_| ())
            }) {
                change["error"] = json!(format!("Wi-Fi environment applied but not saved: {error}"));
            }
        }
        Err(error) => {
            change["status"] = json!("rejected");
            change["error"] = json!(error.to_string());
        }
    }
    state["wifi_environment_change"] = change;
}

pub struct Runtime {
    catalog: Catalog,
    id: String,
    identity: Value,
    session: Option<Session>,
    last_state: Value,
    last_frame: Option<Frame>,
    last_uart: Vec<transport::Output>,
    pending_camera: Option<(u64, u64, CameraSource)>,
    pending_access_point: Option<(u64, bool)>,
    pending_wifi_environment: Option<(u64, String, Value)>,
    ports: BTreeMap<u8, SerialPort>,
    uart_captures: BTreeMap<u8, crate::uart_capture::Capture>,
    pub shutdown: bool,
}
impl Runtime {
    pub fn new(catalog: Catalog, id: String) -> Result<Self> {
        let item = catalog.device(&id)?;
        let mut ports = BTreeMap::new();
        if let Some(channels) = item["host"]["uart"].as_array() {
            for channel in channels {
                ports.insert(
                    channel.as_u64().context("Invalid UART channel")? as u8,
                    SerialPort::new()?,
                );
            }
        }
        Ok(Self {
            catalog,
            id,
            identity: json!({"id":Uuid::new_v4().simple().to_string(),
                "pid":std::process::id(),"build":crate::identity::build()}),
            session: None,
            last_state: Value::Null,
            last_frame: None,
            last_uart: Vec::new(),
            pending_camera: None,
            pending_access_point: None,
            pending_wifi_environment: None,
            ports,
            uart_captures: BTreeMap::new(),
            shutdown: false,
        })
    }
    pub fn tick(&mut self) -> Result<()> {
        if let Some(session) = &mut self.session {
            if !session.finished
                && let Some((id, result)) = session.qmp.recover(&mut session.uart)?
            {
                if self.pending_wifi_environment.as_ref().is_some_and(|(pending, _, _)| *pending == id) {
                    let (_, request, environment) = self.pending_wifi_environment.take().unwrap();
                    finish_wifi_environment_change(&self.catalog, &self.id, &mut session.state,
                        &request, environment, result.map(|_| ()));
                } else if self
                    .pending_access_point
                    .is_some_and(|(pending, _)| pending == id)
                {
                    let (_, present) = self.pending_access_point.take().unwrap();
                    finish_access_point_change(
                        &self.catalog,
                        &self.id,
                        &mut session.state,
                        present,
                        result.map(|_| ()),
                    );
                } else if self
                    .pending_camera
                    .as_ref()
                    .is_some_and(|(pending, _, _)| *pending == id)
                {
                    let (_, generation, source) = self.pending_camera.take().unwrap();
                    session
                        .camera
                        .as_mut()
                        .context("Camera input map is missing")?
                        .confirmed(generation, result.is_ok())?;
                    session.selected_camera(&source, result.is_ok());
                    finish_camera_change(
                        &self.catalog,
                        &self.id,
                        &mut session.state,
                        source,
                        result,
                    );
                }
            }
            if !session.finished && session.qmp.pending_id().is_none() {
                if session.pending_cancel {
                    session.qmp.set(
                        "x-lisa-button-sequence",
                        json!("cancel"),
                        &mut session.uart,
                    )?;
                    session.pending_cancel = false;
                }
                while let Some((button, property)) = session
                    .pending_releases
                    .first_key_value()
                    .map(|(button, property)| (button.clone(), property.clone()))
                {
                    session
                        .qmp
                        .set(&property, json!(false), &mut session.uart)?;
                    session.state["controls"]["buttons"][&button] = json!(false);
                    session.pending_releases.remove(&button);
                }
            }
            if !session.finished
                && let Some(audio) = session.audio.as_mut()
                && let Some((enabled, since)) = audio.pending_microphone
            {
                match audio.replies.try_recv() {
                    Ok(reply) => {
                        audio.pending_microphone = None;
                        let actual = reply["microphone"]
                            .as_bool()
                            .context("Invalid microphone response")?;
                        session.options.microphone = actual;
                        session.state["microphone"] = json!(actual);
                        let result = if reply["ok"] == true && actual == enabled {
                            let item = self.catalog.device(&self.id)?;
                            self.catalog
                                .update(
                                    Path::new(item["path"].as_str().unwrap()),
                                    &json!({"microphone":actual}),
                                )
                                .map(|_| ())
                        } else {
                            Err(anyhow::anyhow!(
                                "Host microphone change failed; check device access and microphone permission"
                            ))
                        };
                        session.state["microphone_change"] = match result {
                            Ok(()) => json!({"status":"applied","enabled":actual}),
                            Err(error) => {
                                json!({"status":"error","enabled":enabled,"error":error.to_string()})
                            }
                        };
                    }
                    Err(std::sync::mpsc::TryRecvError::Empty)
                        if since.elapsed() < Duration::from_secs(40) => {}
                    Err(_) => {
                        // Retain ownership until the same request is confirmed.
                        // A late reply cannot acknowledge any later request.
                        session.state["microphone_change"] = json!({"status":"unknown","enabled":enabled,"error":"Waiting for host microphone confirmation"});
                    }
                }
            }
            Self::tick_camera(&self.catalog, &self.id, &mut self.pending_camera, session)?;
            session.tick()?;
            if session.finished && let Some((_, request, _)) = self.pending_wifi_environment.take() {
                session.state["wifi_environment_change"] = json!({"request":request,"status":"unknown",
                    "error":"Runtime ended before Wi-Fi environment change was confirmed"});
            }
            if session.finished && self.pending_access_point.take().is_some() {
                session.state["access_point_change"] = json!({"status":"unknown",
                    "error":"Runtime ended before access point change was confirmed"});
            }
        }
        for (&channel, port) in &mut self.ports {
            port.pump(
                self.session
                    .as_mut()
                    .filter(|s| !s.finished)
                    .map(|s| &mut s.uart[channel as usize]),
            )?;
        }
        if self.session.as_ref().is_some_and(|s| s.finished) {
            let mut session = self.session.take().unwrap();
            if self.pending_camera.take().is_some() {
                session.state["camera_change"]["status"] = json!("unknown");
                session.state["camera_change"]["error"] =
                    json!("Runtime ended before camera input outcome could be confirmed");
            }
            self.last_uart = session.uart.iter().map(Uart::observer).collect();
            self.last_frame = session.frame();
            self.last_state = session.state.clone();
        }
        Ok(())
    }
    fn tick_camera(
        catalog: &Catalog,
        id: &str,
        pending: &mut Option<(u64, u64, CameraSource)>,
        session: &mut Session,
    ) -> Result<()> {
        if session.finished {
            return Ok(());
        }
        session.disconnect_camera_input()?;
        // Existing source keeps running while a replacement awaits permission,
        // its first frame or QMP acknowledgement. The two mailboxes never mix.
        if let Some(capture) = &mut session.camera_capture {
            let input = session
                .camera
                .as_mut()
                .context("Camera input map is missing")?;
            if capture.ended() {
                if !capture.disconnected_published {
                    capture.disconnected_published =
                        input.publish(None, crate::camera_input::SourceState::Disconnected, 0)?;
                }
                let _ = capture.take();
            } else if let Some(frame) = capture.take() {
                if !input.publish(
                    Some(&frame.rgb),
                    crate::camera_input::SourceState::Live,
                    frame.host_ns,
                )? {
                    capture.dropped();
                }
            }
            if session.camera_candidate.is_none() {
                session.state["camera_capture"] = capture.status(false);
            }
        }
        if pending.is_some() {
            return Ok(());
        }
        let Some(candidate) = &mut session.camera_candidate else {
            return Ok(());
        };
        session.state["camera_capture"] = candidate.status(true);
        if candidate.ended()
            || candidate.started.elapsed() >= crate::camera_capture::FIRST_FRAME_TIMEOUT
        {
            let source = CameraSource {
                path: None,
                device: Some(candidate.device_id.clone()),
            };
            let error = candidate
                .error()
                .unwrap_or_else(|| "Host camera did not deliver a frame within 120 seconds".into());
            session.state["camera_capture"]["state"] = json!("error");
            session.state["camera_capture"]["error"] = json!(error);
            finish_camera_change(
                catalog,
                id,
                &mut session.state,
                source,
                Err(anyhow::anyhow!(error)),
            );
            session.camera_candidate = None;
            return Ok(());
        }
        if session.qmp.pending_id().is_some() {
            return Ok(());
        }
        let Some(frame) = candidate.take() else {
            return Ok(());
        };
        let source = CameraSource {
            path: None,
            device: Some(candidate.device_id.clone()),
        };
        let input = session
            .camera
            .as_mut()
            .context("Camera input map is missing")?;
        let generation = input.prepare(
            crate::camera_input::SourceState::Live,
            Some(&frame.rgb),
            frame.host_ns,
        )?;
        match session.qmp.set_camera_source(generation, &mut session.uart) {
            Ok(()) => {
                input.confirmed(generation, true)?;
                session.selected_camera(&source, true);
                finish_camera_change(catalog, id, &mut session.state, source, Ok(Value::Null));
            }
            Err(error) if error.is::<transport::ControlPending>() => {
                session.state["camera_change"]["phase"] = json!("selecting");
                *pending = Some((session.qmp.pending_id().unwrap(), generation, source));
            }
            Err(error) => {
                if session.qmp.pending_id().is_none() {
                    input.confirmed(generation, false)?;
                    session.selected_camera(&source, false);
                    finish_camera_change(catalog, id, &mut session.state, source, Err(error));
                } else {
                    session.state["camera_change"]["status"] = json!("unknown");
                    session.state["camera_change"]["error"] = json!(error.to_string());
                    return Err(error);
                }
            }
        }
        Ok(())
    }
    pub fn status(&self) -> Result<Value> {
        let item = self.catalog.device(&self.id)?;
        let mut state = self
            .session
            .as_ref()
            .map(|s| s.state.clone())
            .unwrap_or_else(|| self.last_state.clone());
        if state.is_object() {
            state["device_id"] = json!(self.id);
            state["finished"] = json!(self.session.is_none());
            state["lifecycle"] = json!(if self.session.is_some() {
                "on"
            } else if state["error"].is_null() {
                "off"
            } else {
                "failed"
            });
            state["guest_fault"] = json!(
                state["exceptions"].as_u64().unwrap_or(0) > 0
                    || state["ap_exceptions"].as_u64().unwrap_or(0) > 0
            );
            let mut indicators = json!({});
            if let Some(list) = item["hardware"]["board"]["indicators"].as_array() {
                for indicator in list {
                    let name = indicator["id"].as_str().context("Indicator ID missing")?;
                    let bank = indicator["bank"]
                        .as_str()
                        .context("Indicator bank missing")?;
                    let pin = indicator["pin"].as_u64().context("Indicator pin missing")?;
                    ensure!(pin < 32, "Invalid indicator pin");
                    indicators[name] = if self.session.is_none() {
                        json!(false)
                    } else if state["pads"][bank]["driven"].as_u64().unwrap_or(0) & (1 << pin) != 0
                    {
                        json!(
                            (state["pads"][bank]["levels"].as_u64().unwrap_or(0) & (1 << pin) != 0)
                                != indicator["active_low"].as_bool().unwrap_or(false)
                        )
                    } else {
                        Value::Null
                    };
                }
            }
            state["power_hold"] = indicators.get("power-hold").cloned().unwrap_or(Value::Null);
            state["runtime_active"] = json!(self.session.is_some());
            state["indicators"] = indicators;
        }
        let ports: BTreeMap<_, _> = self
            .ports
            .iter()
            .map(|(n, p)| (n.to_string(), p.path.clone()))
            .collect();
        let captures: BTreeMap<_, _> = self.uart_captures.iter()
            .map(|(channel, capture)| (channel.to_string(), capture.status())).collect();
        Ok(json!({"session":state,"serial":ports,"uart_captures":captures,"worker":self.identity}))
    }
    pub fn call(&mut self, method: &str, params: &Value) -> Result<Value> {
        if let Some(run) = params.get("run").filter(|v| !v.is_null()) {
            ensure!(
                self.session
                    .as_ref()
                    .map(|s| json!(s.output))
                    .unwrap_or_else(|| self.last_state["output"].clone())
                    == *run,
                "The request belongs to an earlier run"
            );
        }
        match method {
            "status" => self.status(),
            "start" => {
                ensure!(self.session.is_none(), "Instance is already running");
                self.pending_access_point = None;
                self.pending_wifi_environment = None;
                let options: Options = serde_json::from_value(params["options"].clone())?;
                let mut session =
                    Session::start(&self.catalog, &self.catalog.device(&self.id)?, options)?;
                for (channel, uart) in session.uart.iter_mut().enumerate() {
                    if let Some(port) = self.ports.get_mut(&(channel as u8)) {
                        port.bind(uart.output())?;
                    } else {
                        uart.discard_output();
                    }
                }
                self.session = Some(session);
                self.status()
            }
            "stop" => {
                if let Some(mut session) = self.session.take() {
                    session.stop()?;
                    if let Some((_, request, _)) = self.pending_wifi_environment.take() {
                        session.state["wifi_environment_change"] = json!({"request":request,"status":"unknown",
                            "error":"Runtime stopped before Wi-Fi environment change was confirmed"});
                    }
                    if self.pending_access_point.take().is_some() {
                        session.state["access_point_change"] = json!({"status":"unknown",
                            "error":"Runtime stopped before access point change was confirmed"});
                    }
                    if self.pending_camera.take().is_some() {
                        session.state["camera_change"]["status"] = json!("unknown");
                        session.state["camera_change"]["error"] =
                            json!("Runtime stopped before camera input outcome could be confirmed");
                    }
                    self.last_uart = session.uart.iter().map(Uart::observer).collect();
                    self.last_frame = session.frame();
                    self.last_state = session.state.clone();
                }
                self.tick()?;
                self.status()
            }
            "reset" | "reset_download" => {
                let mut options = self
                    .session
                    .as_ref()
                    .context("Power on the instance before resetting")?
                    .options
                    .clone();
                self.call("stop", &json!({}))?;
                options.download = method == "reset_download";
                self.call("start", &json!({"options":options}))
            }
            "button" => {
                let session = self.session.as_mut().context("Instance is not running")?;
                let property = session
                    .backend
                    .button(params["button"].as_str().context("Missing button")?)?;
                let pressed = params["pressed"]
                    .as_bool()
                    .context("A button requires an explicit pressed state")?;
                if !pressed && session.qmp.pending_id().is_some() {
                    // Preserve GUI/connection cleanup while a camera transfer
                    // owns QMP. The run owns this bounded release latch.
                    session.pending_releases.insert(
                        params["button"].as_str().unwrap().to_owned(),
                        property.to_owned(),
                    );
                    bail!(
                        "Button release is pending QEMU control recovery; completion is not yet confirmed"
                    );
                }
                session
                    .qmp
                    .set(property, json!(pressed), &mut session.uart)?;
                session.state["controls"]["buttons"][params["button"].as_str().unwrap()] =
                    json!(pressed);
                Ok(json!(true))
            }
            "touch" => {
                let session = self.session.as_mut().context("Instance is not running")?;
                ensure!(params["run"].is_string(), "Touch input requires a run identifier");
                let x = params["x"].as_u64().context("Touch X is required")?;
                let y = params["y"].as_u64().context("Touch Y is required")?;
                ensure!(x < 240 && y < 320, "Touch coordinates are outside CST328 bounds");
                let pressed = params["pressed"].as_bool().context("Touch state is required")?;
                let property = session.backend.touch()?;
                let value = if pressed {
                    format!("{x},{y},press")
                } else {
                    "release".to_owned()
                };
                session.qmp.set(property, json!(value), &mut session.uart)?;
                Ok(json!({"x":x,"y":y,"pressed":pressed}))
            }
            "button_sequence" => {
                ensure!(
                    params["run"].is_string(),
                    "Button sequences require a run identifier"
                );
                let request: ButtonSequence = serde_json::from_value(params.clone())?;
                let command = request.command()?;
                let session = self.session.as_mut().context("Instance is not running")?;
                session.backend.button(&request.button)?;
                session
                    .qmp
                    .set("x-lisa-button-sequence", json!(command), &mut session.uart)?;
                match session.button_sequence() {
                    Ok(state) => Ok(state),
                    Err(error) => {
                        // The sequence was submitted, but its identifier was not
                        // delivered to the owner. Cancel before admitting input.
                        if session.qmp.pending_id().is_some() {
                            session.pending_cancel = true;
                        } else {
                            session.qmp.set(
                                "x-lisa-button-sequence",
                                json!("cancel"),
                                &mut session.uart,
                            )?;
                        }
                        Err(error.context(
                            "Button sequence identity was not confirmed; cancellation requested",
                        ))
                    }
                }
            }
            "button_sequence_status" | "button_sequence_cancel" => {
                ensure!(
                    params["run"].is_string(),
                    "Button sequences require a run identifier"
                );
                let expected = params["sequence"]
                    .as_u64()
                    .filter(|id| *id > 0)
                    .context("A positive sequence identifier is required")?;
                if method == "button_sequence_cancel"
                    && let Some(session) = &mut self.session
                    && session.qmp.pending_id().is_some()
                {
                    ensure!(
                        session.state["button_sequence"]["sequence"].as_u64() == Some(expected),
                        "The request belongs to an earlier button sequence"
                    );
                    session.pending_cancel = true;
                    bail!(
                        "Button sequence cancellation is pending QEMU control recovery; completion is not yet confirmed"
                    );
                }
                let state = if let Some(session) = &mut self.session {
                    session.button_sequence()?
                } else {
                    self.last_state["button_sequence"].clone()
                };
                ensure!(
                    state["sequence"].as_u64() == Some(expected),
                    "The request belongs to an earlier button sequence"
                );
                if method == "button_sequence_cancel" {
                    if let Some(session) = &mut self.session {
                        session.qmp.set(
                            "x-lisa-button-sequence",
                            json!("cancel"),
                            &mut session.uart,
                        )?;
                        return session.button_sequence();
                    }
                }
                Ok(state)
            }
            "wifi_environment" => {
                let value = params.get("environment").context("Missing Wi-Fi environment")?;
                let environment = serde_json::to_value(
                    crate::wifi_environment::WifiEnvironment::from_value(value)?)?;
                if let Some(session) = self.session.as_mut().filter(|s| !s.finished) {
                    ensure!(params["run"].is_string(), "Running Wi-Fi changes require a run identifier");
                    session.qmp.ensure_idle()?;
                    let request = Uuid::new_v4().simple().to_string();
                    match session.qmp.set("wifi-environment", json!(environment.to_string()), &mut session.uart) {
                        Err(error) if error.is::<transport::ControlPending>() => {
                            self.pending_wifi_environment = Some((session.qmp.pending_id().unwrap(), request.clone(), environment));
                            session.state["wifi_environment_change"] = json!({"request":request,"status":"pending"});
                        }
                        result => finish_wifi_environment_change(&self.catalog, &self.id,
                            &mut session.state, &request, environment, result),
                    }
                    Ok(session.state["wifi_environment_change"].clone())
                } else {
                    ensure!(params["run"].is_null(), "The request belongs to an earlier run");
                    let item = self.catalog.device(&self.id)?;
                    let path = Path::new(item["path"].as_str().context("Instance path missing")?);
                    let layout = self.catalog.layout(item["board"].as_str().context("Board missing")?)?;
                    let _storage = storage::locked(path, &layout)?;
                    self.catalog.update(path, &json!({"wifi_environment":environment}))?;
                    Ok(json!({"status":"saved"}))
                }
            }
            "access_point" => {
                let present = params["enabled"]
                    .as_bool()
                    .context("Access point presence requires a boolean")?;
                if let Some(session) = self.session.as_mut().filter(|s| !s.finished) {
                    ensure!(
                        params["run"].is_string(),
                        "Running access point changes require a run identifier"
                    );
                    session.qmp.ensure_idle()?;
                    match session
                        .qmp
                        .set("wifi-ap-present", json!(present), &mut session.uart)
                    {
                        Err(error) if error.is::<transport::ControlPending>() => {
                            self.pending_access_point =
                                Some((session.qmp.pending_id().unwrap(), present));
                            session.state["access_point_change"] =
                                json!({"status":"pending","enabled":present});
                        }
                        result => finish_access_point_change(
                            &self.catalog,
                            &self.id,
                            &mut session.state,
                            present,
                            result,
                        ),
                    }
                    Ok(session.state["access_point_change"].clone())
                } else {
                    ensure!(
                        params["run"].is_null(),
                        "The request belongs to an earlier run"
                    );
                    let item = self.catalog.device(&self.id)?;
                    self.catalog.update(
                        Path::new(item["path"].as_str().unwrap()),
                        &json!({"wifi_ap_present":present}),
                    )?;
                    Ok(json!({"status":"saved","enabled":present}))
                }
            }
            "microphone" => {
                let enabled = params["enabled"]
                    .as_bool()
                    .context("Microphone requires a boolean")?;
                ensure!(
                    params["run"].is_null() || self.session.as_ref().is_some_and(|s| !s.finished),
                    "The request belongs to an earlier run"
                );
                if let Some(session) = self.session.as_mut().filter(|s| !s.finished) {
                    ensure!(
                        session.options.capture.is_none(),
                        "Microphone changes are unavailable during diagnostic capture"
                    );
                    ensure!(
                        !enabled || session.state["input_busy"] != true,
                        "WAV input is still being consumed"
                    );
                    let audio = session
                        .audio
                        .as_mut()
                        .context("This run has no host audio endpoint")?;
                    audio.microphone(enabled)?;
                    let change = json!({"status":"pending","enabled":enabled});
                    session.state["microphone_change"] = change.clone();
                    Ok(change)
                } else {
                    let item = self.catalog.device(&self.id)?;
                    self.catalog.update(
                        Path::new(item["path"].as_str().unwrap()),
                        &json!({"microphone":enabled}),
                    )?;
                    Ok(json!({"status":"saved","enabled":enabled}))
                }
            }
            "mute" => {
                if let Some(audio) = self.session.as_mut().and_then(|s| s.audio.as_mut()) {
                    audio.mute(
                        params["muted"]
                            .as_bool()
                            .context("Mute requires a boolean")?,
                    )?;
                }
                Ok(json!(true))
            }
            "camera" => {
                let item = self.catalog.device(&self.id)?;
                ensure!(
                    item["hardware"]["board"]["camera"].is_object(),
                    "This board has no camera input"
                );
                let source = CameraSource::from_params(params)?;
                let rotation = crate::camera::mounting_rotation(&item["hardware"]["board"])?;
                let frame = source
                    .path
                    .as_deref()
                    .map(|path| crate::camera::load(path, rotation))
                    .transpose()?;
                if let Some(session) = self.session.as_mut() {
                    ensure!(
                        params["run"].is_string(),
                        "Running camera input requires a run identifier"
                    );
                    session.qmp.ensure_idle()?;
                    // Cancel any candidate that has not submitted a selection.
                    // An in-flight QMP mutation stays protected by ensure_idle.
                    if source.path.is_none() && source.device.is_none() {
                        session.close_camera_input()?;
                    } else {
                        session.camera_candidate = None;
                    }
                    if let Some(device) = &source.device {
                        let assets = Assets::new(&self.catalog.root)?;
                        match crate::camera_capture::Capture::start(&assets, device, rotation) {
                            Ok(capture) => {
                                session.state["camera_capture"] = capture.status(true);
                                session.camera_candidate = Some(capture);
                                let mut change = source.properties();
                                change["status"] = json!("pending");
                                change["phase"] = json!("waiting-first-frame");
                                session.state["camera_change"] = change.clone();
                                return Ok(change);
                            }
                            Err(error) => {
                                session.state["camera_capture"] = json!({"state":"error","device_id":device,"frames":0,"dropped":0,"last_frame_host_ns":0,"error":error.to_string()});
                                finish_camera_change(
                                    &self.catalog,
                                    &self.id,
                                    &mut session.state,
                                    source,
                                    Err(anyhow::anyhow!(error.to_string())),
                                );
                                return Err(error);
                            }
                        }
                    }
                    let generation = session
                        .camera
                        .as_mut()
                        .context("Camera input map is missing")?
                        .prepare(
                            if frame.is_some() {
                                crate::camera_input::SourceState::Still
                            } else {
                                crate::camera_input::SourceState::Clear
                            },
                            frame.as_deref(),
                            0,
                        )?;
                    if let Err(error) = session.qmp.set_camera_source(generation, &mut session.uart)
                    {
                        if error.is::<transport::ControlPending>() {
                            self.pending_camera = Some((
                                session.qmp.pending_id().unwrap(),
                                generation,
                                source.clone(),
                            ));
                            let mut change = source.properties();
                            change["status"] = json!("pending");
                            change["phase"] = json!("selecting");
                            session.state["camera_change"] = change.clone();
                            return Ok(change);
                        }
                        if session.qmp.pending_id().is_none() {
                            session
                                .camera
                                .as_mut()
                                .unwrap()
                                .confirmed(generation, false)?;
                        }
                        let mut change = source.properties();
                        change["status"] = json!(if session.qmp.pending_id().is_some() {
                            "unknown"
                        } else {
                            "rejected"
                        });
                        change["error"] = json!(error.to_string());
                        session.state["camera_change"] = change;
                        return Err(error);
                    }
                    session
                        .camera
                        .as_mut()
                        .unwrap()
                        .confirmed(generation, true)?;
                    session.selected_camera(&source, true);
                    finish_camera_change(
                        &self.catalog,
                        &self.id,
                        &mut session.state,
                        source,
                        Ok(Value::Null),
                    );
                    let mut result = session.state["camera_change"].clone();
                    result["width"] = json!(crate::camera::WIDTH);
                    result["height"] = json!(crate::camera::HEIGHT);
                    result["fit"] = json!("center-crop");
                    return Ok(result);
                }
                self.catalog.update(
                    Path::new(item["path"].as_str().unwrap()),
                    &source.settings(),
                )?;
                let mut result = source.properties();
                result["status"] = json!("saved");
                result["width"] = json!(crate::camera::WIDTH);
                result["height"] = json!(crate::camera::HEIGHT);
                result["fit"] = json!("center-crop");
                Ok(result)
            }
            "audio_cancel" | "audio_record" => {
                ensure!(
                    params["run"].is_string(),
                    "Audio controls require a run identifier"
                );
                let session = self
                    .session
                    .as_mut()
                    .filter(|s| !s.finished)
                    .context("Instance is not running")?;
                if method == "audio_cancel" {
                    let sequence = params["sequence"]
                        .as_u64()
                        .context("Missing injection sequence")?;
                    session.qmp.set(
                        "x-lisa-audio-input-cancel",
                        json!(sequence.to_string()),
                        &mut session.uart,
                    )?;
                    session.capture()?;
                    Ok(session.state["audio_input"].clone())
                } else {
                    let action = params["action"]
                        .as_str()
                        .context("Missing recording action")?;
                    ensure!(
                        ["start", "read", "stop", "discard"].contains(&action),
                        "Invalid recording action"
                    );
                    if action == "start" {
                        session.qmp.set(
                            "x-lisa-audio-record",
                            json!("start"),
                            &mut session.uart,
                        )?;
                    } else {
                        let sequence = params["sequence"]
                            .as_u64()
                            .context("Missing recording sequence")?;
                        let status = session
                            .qmp
                            .get("x-lisa-audio-record-status", &mut session.uart)?;
                        let status: Value = serde_json::from_str(
                            status.as_str().context("Invalid recording state")?,
                        )?;
                        ensure!(
                            sequence > 0 && status["sequence"] == sequence,
                            "Recording belongs to an earlier session"
                        );
                        if action == "stop" || action == "discard" {
                            session.qmp.set(
                                "x-lisa-audio-record",
                                json!(if action == "discard" {
                                    format!("discard:{sequence}")
                                } else {
                                    sequence.to_string()
                                }),
                                &mut session.uart,
                            )?;
                        }
                    }
                    let data = session.qmp.get("x-lisa-audio-record", &mut session.uart)?;
                    Ok(serde_json::from_str(
                        data.as_str().context("Invalid recording response")?,
                    )?)
                }
            }
            "audio" => {
                ensure!(
                    params["run"].is_string(),
                    "Audio input requires a run identifier"
                );
                let session = self
                    .session
                    .as_mut()
                    .filter(|s| !s.finished)
                    .context("Instance is not running")?;
                ensure!(
                    !session.options.microphone
                        && session
                            .audio
                            .as_ref()
                            .is_none_or(|a| a.pending_microphone.is_none()),
                    "Microphone owns the ADC input for this run"
                );
                let source = Path::new(params["path"].as_str().context("WAV path missing")?);
                let (offset, length) = crate::wave_input::samples(source)?;
                ensure!(session.state["input_busy"] != true, "Previous audio input is still being consumed");
                let source = fs::canonicalize(source)?;
                session.qmp.set("x-lisa-audio-file", json!(json!({"path":source,
                    "offset":offset,"length":length}).to_string()), &mut session.uart)?;
                if session.options.capture.is_some() {
                    fs::copy(&source, session.output.join("live")
                        .join(format!("input-{}.wav", Uuid::new_v4().simple())))?;
                }
                session.capture()?;
                Ok(session.state["audio_input"].clone())
            }
            "uart_capture" => {
                let channel = params["channel"].as_u64().filter(|n| *n < 3).context("UART channel must be 0..2")? as u8;
                let action = params["action"].as_str().context("Capture action missing")?;
                if action == "start" {
                    ensure!(self.uart_captures.get(&channel).is_none_or(|c| c.status()["status"] != "running"),
                        "UART receiver is already active");
                    let port = self.ports.get(&channel).context("Enable this UART endpoint before receiving")?;
                    let path = Path::new(params["path"].as_str().context("Capture path missing")?);
                    let capture = crate::uart_capture::Capture::start(&port.path, path,
                        params["timeout"].as_u64().unwrap_or(36000))?;
                    let status = capture.status();
                    self.uart_captures.insert(channel, capture);
                    return Ok(status);
                }
                ensure!(["status", "stop"].contains(&action), "Invalid capture action");
                let capture = self.uart_captures.get_mut(&channel).context("No UART receiver")?;
                ensure!(params["capture"].is_string() && params["capture"] == capture.status()["capture"],
                    "The request belongs to an earlier UART capture");
                Ok(if action == "stop" { capture.stop() } else { capture.status() })
            }
            "uart_read" => {
                let channel = params["channel"].as_u64().context("Missing UART channel")?;
                ensure!(channel < 3, "UART channel must be 0..2");
                let source = self
                    .session
                    .as_ref()
                    .map(|s| s.uart[channel as usize].observer())
                    .or_else(|| self.last_uart.get(channel as usize).cloned())
                    .context("Instance has not run")?;
                let cursor = params["cursor"].as_u64().unwrap_or(0);
                let limit = params["limit"].as_u64().unwrap_or(4096).try_into()?;
                let mut result = source.lock().unwrap().read(cursor, limit)?;
                result["run"] = self
                    .session
                    .as_ref()
                    .map(|s| json!(s.output))
                    .unwrap_or_else(|| self.last_state["output"].clone());
                Ok(result)
            }
            "uart_write" => {
                let channel = params["channel"].as_u64().context("Missing UART channel")?;
                ensure!(channel < 3, "UART channel must be 0..2");
                let bytes = storage::unhex(params["hex"].as_str().context("Missing UART bytes")?)?;
                ensure!(bytes.len() <= 4096, "UART command exceeds 4096 bytes");
                self.session
                    .as_mut()
                    .context("Instance is not running")?
                    .uart[channel as usize]
                    .send(&bytes)?;
                Ok(json!(true))
            }
            "serial" => {
                if params["enabled"] == false {
                    let channel = params["channel"].as_u64().unwrap_or(255) as u8;
                    ensure!(self.uart_captures.get(&channel).is_none_or(|c| c.status()["status"] != "running"), "Stop the UART receiver before disabling its endpoint");
                }
                let channel = params["channel"].as_u64().unwrap_or(0);
                ensure!(channel < 3, "UART channel must be 0..2");
                let enabled = params["enabled"].as_bool().unwrap_or(true);
                if enabled && !self.ports.contains_key(&(channel as u8)) {
                    let mut port = SerialPort::new()?;
                    if let Some(session) = &mut self.session {
                        port.bind(session.uart[channel as usize].output())?;
                    }
                    self.ports.insert(channel as u8, port);
                } else if !enabled {
                    self.ports.remove(&(channel as u8));
                    if let Some(session) = &mut self.session {
                        session.uart[channel as usize].discard_output();
                    }
                }
                let item = self.catalog.device(&self.id)?;
                self.catalog.update(
                    Path::new(item["path"].as_str().unwrap()),
                    &json!({"uart":self.ports.keys().collect::<Vec<_>>()}),
                )?;
                Ok(self
                    .ports
                    .get(&(channel as u8))
                    .map(|p| json!(p.path))
                    .unwrap_or(Value::Null))
            }
            "screenshot" => {
                let path = params["path"].as_str().map(Path::new);
                let deadline = Instant::now() + Duration::from_secs(1);
                loop {
                    let frame = if let Some(session) = &mut self.session {
                        session.frame()
                    } else {
                        self.last_frame.clone()
                    };
                    if let Some(frame) = frame {
                        if let Some(path) = path {
                            frame.save(path)?;
                            break;
                        }
                        use base64::Engine;
                        let run = self
                            .session
                            .as_ref()
                            .map(|s| json!(s.output))
                            .unwrap_or_else(|| self.last_state["output"].clone());
                        return Ok(
                            json!({"mime_type":"image/png", "width":frame.width, "height":frame.height,
                            "instance_id":self.id,"worker_id":self.identity["id"],"run":run,
                            "frame":{"sequence":frame.sequence,"virtual_ns":frame.virtual_ns,
                                "host_monotonic_ns":frame.host_monotonic_ns},
                            "source":if self.session.is_some() {"active_run"} else {"last_run"},
                            "data":base64::engine::general_purpose::STANDARD.encode(frame.png()?)}),
                        );
                    }
                    ensure!(Instant::now() < deadline, "No display frame available");
                    thread::sleep(Duration::from_millis(5));
                }
                Ok(json!(path))
            }
            "shutdown" => {
                self.call("stop", &json!({}))?;
                self.shutdown = true;
                Ok(json!(true))
            }
            _ => bail!("Unknown runtime method"),
        }
    }
}

/// Private local endpoint; random authentication is independent of firmware UID.
pub struct Client {
    endpoint: Value,
}
impl Client {
    pub fn connect(instance: &Path) -> Result<Self> {
        let endpoint = storage::read_json(&instance.join("runtime.json"))?;
        ensure!(endpoint["version"] == 1, "Unsupported runtime protocol");
        ensure!(
            endpoint["instance_id"] == storage::read_json(&instance.join("device.json"))?["id"],
            "Runtime endpoint belongs to another instance"
        );
        let this = Self { endpoint };
        this.call("status", json!({}))?;
        Ok(this)
    }
    pub fn call(&self, method: &str, params: Value) -> Result<Value> {
        let port = self.endpoint["port"]
            .as_u64()
            .context("Runtime port missing")?;
        ensure!(port > 0 && port <= 65535, "Invalid runtime port");
        let mut stream = TcpStream::connect_timeout(
            &([127, 0, 0, 1], port as u16).into(),
            Duration::from_secs(1),
        )?;
        stream.set_read_timeout(Some(Duration::from_secs(60)))?;
        stream.set_write_timeout(Some(Duration::from_secs(3)))?;
        writeln!(
            stream,
            "{}",
            json!({"token":self.endpoint["token"],"method":method,"params":params})
        )?;
        let mut response = String::new();
        BufReader::new(stream)
            .take(4 * 1024 * 1024)
            .read_line(&mut response)?;
        let reply: Value = serde_json::from_str(&response)?;
        if let Some(error) = reply["error"].as_str() {
            bail!("{error}");
        }
        Ok(reply["result"].clone())
    }
}

pub fn serve(catalog: Catalog, id: String, stop: Arc<AtomicBool>) -> Result<()> {
    let item = catalog.device(&id)?;
    let path = PathBuf::from(item["path"].as_str().context("Missing instance path")?);
    let _lease = Lease::acquire(&path.join("runtime.lock"))?;
    let listener = transport::listener()?;
    let token = Uuid::new_v4().simple().to_string();
    let endpoint = path.join("runtime.json");
    let mut runtime = Runtime::new(catalog, id.clone())?;
    storage::write_json(
        &endpoint,
        &json!({"version":1,"instance_id":id,"port":listener.local_addr()?.port(),"token":token,"pid":std::process::id()}),
    )?;
    struct Connection {
        stream: TcpStream,
        input: Vec<u8>,
        output: Vec<u8>,
        offset: usize,
        deadline: Instant,
    }
    let mut connections: Vec<Connection> = Vec::new();
    while !runtime.shutdown && !stop.load(Ordering::Acquire) {
        if let Err(error) = runtime.tick()
            && let Some(session) = &mut runtime.session
        {
            session.state["error"] = json!(error.to_string());
            let _ = session.stop();
        }
        if connections.len() < 32 {
            match listener.accept() {
                Ok((stream, _)) => {
                    stream.set_nonblocking(true)?;
                    connections.push(Connection {
                        stream,
                        input: Vec::new(),
                        output: Vec::new(),
                        offset: 0,
                        deadline: Instant::now() + Duration::from_secs(5),
                    });
                }
                Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {}
                Err(e) => return Err(e.into()),
            }
        }
        connections.retain_mut(|connection| {
            let result = (|| -> Result<bool> {
                ensure!(
                    Instant::now() < connection.deadline,
                    "Control connection timed out"
                );
                if connection.output.is_empty() {
                    let mut bytes = [0; 8192];
                    match connection.stream.read(&mut bytes) {
                        Ok(0) => return Ok(false),
                        Ok(n) => connection.input.extend_from_slice(&bytes[..n]),
                        Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => return Ok(true),
                        Err(e) => return Err(e.into()),
                    }
                    ensure!(connection.input.len() <= 65536, "Control request too large");
                    if let Some(end) = connection.input.iter().position(|&b| b == b'\n') {
                        let request: Value = serde_json::from_slice(&connection.input[..end])?;
                        ensure!(
                            request["token"].as_str() == Some(&token),
                            "Invalid runtime token"
                        );
                        let result = runtime.call(
                            request["method"].as_str().context("Missing method")?,
                            &request["params"],
                        );
                        let reply = match result {
                            Ok(value) => json!({"result":value}),
                            Err(error) => json!({"error":error.to_string()}),
                        };
                        connection.output = serde_json::to_vec(&reply)?;
                        connection.output.push(b'\n');
                        connection.deadline = Instant::now() + Duration::from_secs(5);
                    }
                }
                if !connection.output.is_empty() {
                    match connection
                        .stream
                        .write(&connection.output[connection.offset..])
                    {
                        Ok(n) => connection.offset += n,
                        Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {}
                        Err(e) => return Err(e.into()),
                    }
                    return Ok(connection.offset < connection.output.len());
                }
                Ok(true)
            })();
            result.unwrap_or(false)
        });
        thread::sleep(Duration::from_millis(2));
    }
    runtime.call("stop", &json!({}))?;
    let _ = fs::remove_file(endpoint);
    Ok(())
}

#[cfg(test)]
mod tests {
    #[test]
    fn qemu_error_preserves_fixed_tx_diagnostic_without_raw_stderr() {
        let mmio = "ARCS unsupported write hart=1 pc=0x30600000 address=0x4b708180 size=4 value=0x20001000";
        let reason = "ARCS Wi-Fi AC1: invalid frame length";
        assert_eq!(
            super::qemu_failure(&format!(
                "private guest output\nqemu: {reason}\nqemu: {mmio}"
            )),
            format!("{reason}; qemu: {mmio}")
        );
        assert_eq!(
            super::qemu_failure(&format!("private guest output\n{mmio}")),
            mmio
        );
        assert_eq!(super::qemu_failure("private guest output"), "QEMU failed");
        assert_eq!(super::qemu_failure(reason), reason);
        let long = format!(
            "ARCS Wi-Fi AC3: {}\nARCS {}",
            "x".repeat(4000),
            "界".repeat(4000)
        );
        assert_eq!(super::qemu_failure(&long).chars().count(), 512 + 2 + 2048);
    }

    use super::*;

    #[test]
    fn camera_confirmation_persists_only_acknowledged_input() {
        let temp = tempfile::tempdir().unwrap();
        let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..");
        let catalog = Catalog::open(&root, &temp.path().join("library")).unwrap();
        let item = catalog.create("arcs-mini", None, Some("test")).unwrap();
        let id = item["id"].as_str().unwrap();
        let old = temp.path().join("old.png");
        let new = temp.path().join("new.png");
        catalog
            .update(
                Path::new(item["path"].as_str().unwrap()),
                &json!({"camera_image":old}),
            )
            .unwrap();
        let mut state = json!({"camera_image":old,"camera_change":{"status":"pending","path":new}});
        finish_camera_change(
            &catalog,
            id,
            &mut state,
            CameraSource {
                path: Some(new.clone()),
                device: None,
            },
            Err(anyhow::anyhow!("rejected")),
        );
        assert_eq!(state["camera_change"]["status"], "rejected");
        assert_eq!(state["camera_image"], json!(old));
        assert_eq!(
            catalog.device(id).unwrap()["host"]["camera_image"],
            json!(old)
        );
        finish_camera_change(
            &catalog,
            id,
            &mut state,
            CameraSource {
                path: Some(new.clone()),
                device: None,
            },
            Ok(json!({})),
        );
        assert_eq!(state["camera_change"]["status"], "applied");
        assert_eq!(
            catalog.device(id).unwrap()["host"]["camera_image"],
            json!(new)
        );
        finish_camera_change(
            &catalog,
            id,
            &mut state,
            CameraSource::default(),
            Ok(json!({})),
        );
        assert!(catalog.device(id).unwrap()["host"]["camera_image"].is_null());
        finish_camera_change(
            &catalog,
            "missing",
            &mut state,
            CameraSource {
                path: Some(new.clone()),
                device: None,
            },
            Ok(json!({})),
        );
        assert_eq!(state["camera_change"]["status"], "applied");
        assert!(
            state["camera_change"]["error"]
                .as_str()
                .unwrap()
                .contains("not saved")
        );
        assert_eq!(state["camera_image"], json!(new));
    }

    #[test]
    fn live_selection_persists_only_after_ack_and_clears_image() {
        let temp = tempfile::tempdir().unwrap();
        let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..");
        let catalog = Catalog::open(&root, &temp.path().join("library")).unwrap();
        let item = catalog.create("arcs-mini", None, Some("test")).unwrap();
        let id = item["id"].as_str().unwrap();
        let path = Path::new(item["path"].as_str().unwrap());
        catalog
            .update(path, &json!({"camera_image":"old.png"}))
            .unwrap();
        let source = CameraSource {
            path: None,
            device: Some("camera-a".into()),
        };
        let mut state = json!({"camera_image":"old.png","camera_capture":{"state":"starting"}});
        finish_camera_change(
            &catalog,
            id,
            &mut state,
            source.clone(),
            Err(anyhow::anyhow!("denied")),
        );
        assert_eq!(
            catalog.device(id).unwrap()["host"]["camera_image"],
            "old.png"
        );
        assert!(catalog.device(id).unwrap()["host"]["camera_device"].is_null());
        assert_eq!(state["camera_capture"]["state"], "error");
        finish_camera_change(&catalog, id, &mut state, source, Ok(Value::Null));
        let saved = catalog.device(id).unwrap();
        assert_eq!(saved["host"]["camera_device"], "camera-a");
        assert!(saved["host"]["camera_image"].is_null());
        assert!(
            catalog
                .update(path, &json!({"camera_image":"invalid.png"}))
                .is_err()
        );
        finish_camera_change(
            &catalog,
            id,
            &mut state,
            CameraSource::default(),
            Ok(Value::Null),
        );
        assert!(catalog.device(id).unwrap()["host"]["camera_device"].is_null());
        assert!(CameraSource::from_params(&json!({"path":null,"device_id":"camera-a"})).is_err());
        assert!(CameraSource::from_params(&json!({"device_id":""})).is_err());
        assert!(CameraSource::from_params(&json!({"path":42})).is_err());
    }


}
