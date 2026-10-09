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
        Ok(format!("{},{},{}", self.count, self.hold_ms, self.gap_ms))
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
            .arg((options.timeout + 50).min(900).to_string())
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
        let bytes = Arc::new(Mutex::new(Vec::new()));
        let tail = bytes.clone();
        let reader = thread::spawn(move || {
            let mut reader = BufReader::new(stdout);
            let mut line = String::new();
            let result = reader.read_line(&mut line).map(|_| line);
            let _ = tx.send(result);
            PipeLog::collect(reader, &tail);
        });
        let mut audio = Self {
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
            (5..=600).contains(&options.seconds) && options.timeout > 0 && options.timeout <= 850,
            "Invalid runtime time budget"
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
        command.stdin(Stdio::null());
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
        self.state["controls"]["buttons"]["function"] = state["pressed"].clone();
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
                    self.state["error"] = json!(
                        errors
                            .lines()
                            .rev()
                            .find(|s| s.contains("ARCS"))
                            .unwrap_or("QEMU failed")
                    );
                }
            } else if !self.stopped && self.state["error"].is_null() {
                self.state["error"] = json!("QEMU exited without a readable report");
            }
            if !status.success() && !self.stopped && self.state["error"].is_null() {
                self.state["error"] = json!(format!("QEMU exited with {status}"));
            }
            if self.state["button_sequence"]["status"] == "running" {
                // Without a final report, completion cannot be inferred.
                self.state["button_sequence"]["status"] = json!("interrupted");
                self.state["button_sequence"]["reason"] = json!("runtime-ended");
            }
            if self.state["button_sequence"].is_object() {
                self.state["button_sequence"]["pressed"] = json!(false);
            }
            self.state["controls"]["buttons"]["function"] = json!(false);
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
    fn stop(&mut self) -> Result<()> {
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

fn finish_camera_change(
    catalog: &Catalog,
    id: &str,
    state: &mut Value,
    path: Option<PathBuf>,
    result: Result<Value>,
) {
    match result {
        Ok(_) => {
            state["camera_image"] = json!(path);
            let saved = catalog.device(id).and_then(|item| {
                catalog.update(
                    Path::new(item["path"].as_str().unwrap()),
                    &json!({"camera_image":path}),
                )
            });
            state["camera_change"] = match saved {
                Ok(_) => json!({"status":"applied","path":path}),
                Err(error) => {
                    json!({"status":"applied","path":path,"error":format!("Camera input applied but configuration was not saved: {error}")})
                }
            };
        }
        Err(error) => {
            state["camera_change"] =
                json!({"status":"rejected","path":path,"error":error.to_string()})
        }
    }
}

pub struct Runtime {
    catalog: Catalog,
    id: String,
    identity: Value,
    session: Option<Session>,
    last_state: Value,
    last_frame: Option<Frame>,
    last_uart: Vec<transport::Output>,
    pending_camera: Option<(u64, u64, Option<PathBuf>)>,
    ports: BTreeMap<u8, SerialPort>,
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
            ports,
            shutdown: false,
        })
    }
    pub fn tick(&mut self) -> Result<()> {
        if let Some(session) = &mut self.session {
            if !session.finished
                && let Some((id, result)) = session.qmp.recover(&mut session.uart)?
            {
                if self
                    .pending_camera
                    .as_ref()
                    .is_some_and(|(pending, _, _)| *pending == id)
                {
                    let (_, generation, path) = self.pending_camera.take().unwrap();
                    session
                        .camera
                        .as_mut()
                        .context("Camera input map is missing")?
                        .confirmed(generation, result.is_ok())?;
                    finish_camera_change(&self.catalog, &self.id, &mut session.state, path, result);
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
            session.tick()?;
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
            state["indicators"] = indicators;
        }
        let ports: BTreeMap<_, _> = self
            .ports
            .iter()
            .map(|(n, p)| (n.to_string(), p.path.clone()))
            .collect();
        Ok(json!({"session":state,"serial":ports,"worker":self.identity}))
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
                let value = params
                    .get("path")
                    .context("Camera image path or null is required")?;
                ensure!(
                    value.is_null() || value.is_string(),
                    "Camera image path must be a string or null"
                );
                let path = value
                    .as_str()
                    .map(|p| std::path::absolute(Path::new(p)))
                    .transpose()?;
                let rotation = crate::camera::mounting_rotation(&item["hardware"]["board"])?;
                let frame = path
                    .as_deref()
                    .map(|path| crate::camera::load(path, rotation))
                    .transpose()?;
                if let Some(session) = self.session.as_mut() {
                    ensure!(
                        params["run"].is_string(),
                        "Running camera input requires a run identifier"
                    );
                    session.qmp.ensure_idle()?;
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
                    if let Err(error) = session.qmp.set(
                        "x-lisa-camera-source",
                        json!(generation.to_string()),
                        &mut session.uart,
                    ) {
                        if error.is::<transport::ControlPending>() {
                            self.pending_camera =
                                Some((session.qmp.pending_id().unwrap(), generation, path.clone()));
                            session.state["camera_change"] =
                                json!({"status":"pending","path":path});
                            return Ok(
                                json!({"status":"pending","path":path,"width":crate::camera::WIDTH,"height":crate::camera::HEIGHT,"fit":"center-crop"}),
                            );
                        }
                        if session.qmp.pending_id().is_none() {
                            session
                                .camera
                                .as_mut()
                                .unwrap()
                                .confirmed(generation, false)?;
                        }
                        session.state["camera_change"] = json!({"status":if session.qmp.pending_id().is_some() { "unknown" } else { "rejected" },"path":path,"error":error.to_string()});
                        return Err(error);
                    }
                    session
                        .camera
                        .as_mut()
                        .unwrap()
                        .confirmed(generation, true)?;
                    finish_camera_change(
                        &self.catalog,
                        &self.id,
                        &mut session.state,
                        path,
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
                    &json!({"camera_image":path}),
                )?;
                Ok(
                    json!({"status":"saved","path":path,"width":crate::camera::WIDTH,"height":crate::camera::HEIGHT,"fit":"center-crop"}),
                )
            }
            "audio" => {
                let session = self.session.as_mut().context("Instance is not running")?;
                ensure!(
                    !session.options.microphone,
                    "Microphone owns the ADC input for this run"
                );
                let source = Path::new(params["path"].as_str().context("WAV path missing")?);
                ensure!(
                    fs::metadata(source)?.len() <= 16000 * 2 * 60 + 4096,
                    "Audio input exceeds 60 seconds"
                );
                let bytes = fs::read(source)?;
                let pcm = wave_pcm(&bytes)?;
                let target = session.output.join("live/input.pcm");
                ensure!(
                    session.state["input_busy"] != true,
                    "Previous audio input is still being consumed"
                );
                storage::atomic_write(&target, &pcm)?;
                session
                    .qmp
                    .set("x-lisa-audio-input", json!(target), &mut session.uart)?;
                if session.options.capture.is_some() {
                    storage::atomic_write(
                        &session
                            .output
                            .join("live")
                            .join(format!("input-{}.wav", Uuid::new_v4().simple())),
                        &bytes,
                    )?;
                } else {
                    fs::remove_file(&target)?;
                }
                session.state["input_busy"] = json!(true);
                Ok(json!(true))
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

fn wave_pcm(bytes: &[u8]) -> Result<Vec<u8>> {
    ensure!(
        bytes.len() >= 12 && &bytes[..4] == b"RIFF" && &bytes[8..12] == b"WAVE",
        "Invalid WAV input"
    );
    let size = u32::from_le_bytes(bytes[4..8].try_into()?) as usize;
    ensure!(
        size.checked_add(8).is_some_and(|n| n <= bytes.len()),
        "Truncated WAV input"
    );
    let mut at = 12;
    let mut format = false;
    let mut pcm = None;
    while at + 8 <= size + 8 {
        let length = u32::from_le_bytes(bytes[at + 4..at + 8].try_into()?) as usize;
        let end = (at + 8)
            .checked_add(length)
            .context("Invalid WAV chunk size")?;
        ensure!(end <= size + 8, "Truncated WAV chunk");
        if &bytes[at..at + 4] == b"fmt " {
            ensure!(length >= 16, "Invalid WAV format");
            let value = &bytes[at + 8..end];
            ensure!(
                u16::from_le_bytes(value[0..2].try_into()?) == 1
                    && u16::from_le_bytes(value[2..4].try_into()?) == 1
                    && u32::from_le_bytes(value[4..8].try_into()?) == 16000
                    && u16::from_le_bytes(value[12..14].try_into()?) == 2
                    && u16::from_le_bytes(value[14..16].try_into()?) == 16,
                "Input must be mono PCM16 WAV at 16000 Hz"
            );
            format = true;
        } else if &bytes[at..at + 4] == b"data" {
            ensure!(
                pcm.is_none() && length > 0 && length <= 16000 * 2 * 60 && length.is_multiple_of(2),
                "Invalid WAV sample count"
            );
            pcm = Some(bytes[at + 8..end].to_vec());
        }
        at = end + (length & 1);
    }
    ensure!(format, "Missing WAV format");
    pcm.context("Missing WAV samples")
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
    use super::*;

    fn wave(rate: u32, samples: &[u8]) -> Vec<u8> {
        let mut bytes = b"RIFF".to_vec();
        bytes.extend((36 + samples.len() as u32).to_le_bytes());
        bytes.extend(b"WAVEfmt ");
        bytes.extend(16_u32.to_le_bytes());
        bytes.extend(1_u16.to_le_bytes());
        bytes.extend(1_u16.to_le_bytes());
        bytes.extend(rate.to_le_bytes());
        bytes.extend((rate * 2).to_le_bytes());
        bytes.extend(2_u16.to_le_bytes());
        bytes.extend(16_u16.to_le_bytes());
        bytes.extend(b"data");
        bytes.extend((samples.len() as u32).to_le_bytes());
        bytes.extend(samples);
        bytes
    }

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
            Some(new.clone()),
            Err(anyhow::anyhow!("rejected")),
        );
        assert_eq!(state["camera_change"]["status"], "rejected");
        assert_eq!(state["camera_image"], json!(old));
        assert_eq!(
            catalog.device(id).unwrap()["host"]["camera_image"],
            json!(old)
        );
        finish_camera_change(&catalog, id, &mut state, Some(new.clone()), Ok(json!({})));
        assert_eq!(state["camera_change"]["status"], "applied");
        assert_eq!(
            catalog.device(id).unwrap()["host"]["camera_image"],
            json!(new)
        );
        finish_camera_change(&catalog, id, &mut state, None, Ok(json!({})));
        assert!(catalog.device(id).unwrap()["host"]["camera_image"].is_null());
        finish_camera_change(
            &catalog,
            "missing",
            &mut state,
            Some(new.clone()),
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
    fn wav_input_preserves_samples_and_rejects_invalid_geometry() {
        let samples = [0, 128, 255, 127, 1, 0, 255, 255];
        let valid = wave(16000, &samples);
        assert_eq!(wave_pcm(&valid).unwrap(), samples);
        assert!(wave_pcm(&valid[..valid.len() - 1]).is_err());
        assert!(wave_pcm(&wave(48000, &samples)).is_err());
        assert!(wave_pcm(&wave(16000, &[])).is_err());
        assert!(wave_pcm(&wave(16000, &[1, 2, 3])).is_err());
        assert!(wave_pcm(&wave(16000, &vec![0; 16000 * 2 * 61])).is_err());
    }
}
