//! Front-end-neutral management API. Each running instance has its own worker;
//! front ends choose whether their runs survive disconnection.
use crate::{
    catalog::Catalog,
    runtime::{Client, Options},
    storage,
};
use anyhow::{Context, Result, bail, ensure};
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    fs::File,
    io::{BufRead, Write},
    path::{Path, PathBuf},
    process::{Child, Stdio},
    thread,
    time::{Duration, Instant},
};

const OBSERVATION_CONCURRENCY: usize = 8;

#[derive(Default)]
struct Observations {
    values: BTreeMap<String, Value>,
    errors: BTreeMap<String, String>,
    next: usize,
}

pub struct Manager {
    pub catalog: Catalog,
    executable: PathBuf,
    owned: BTreeMap<String, Child>,
    runs: BTreeMap<String, Value>,
    selected: Option<String>,
    available_qemu_sha256: Option<String>,
    observed: std::sync::Mutex<Observations>,
}
impl Manager {
    pub fn new(root: &Path, data: &Path, executable: &Path) -> Result<Self> {
        Ok(Self {
            catalog: Catalog::open(root, data)?,
            executable: executable.to_owned(),
            owned: BTreeMap::new(),
            runs: BTreeMap::new(),
            selected: None,
            observed: std::sync::Mutex::new(Observations::default()),
            available_qemu_sha256: crate::assets::Assets::new(root)
                .and_then(|assets| storage::sha256(&assets.qemu()))
                .ok(),
        })
    }
    pub fn restore_ports(&mut self) -> Result<()> {
        for item in self.catalog.devices()? {
            if item["host"]["uart"]
                .as_array()
                .is_some_and(|v| !v.is_empty())
            {
                self.worker(item["id"].as_str().context("Missing ID")?)?;
            }
        }
        Ok(())
    }
    fn worker(&mut self, id: &str) -> Result<Client> {
        let item = self.catalog.device(id)?;
        let path = Path::new(item["path"].as_str().unwrap());
        match Client::connect_for_control(path) {
            Ok(client) => return Ok(client),
            Err(error) if error.chain().any(|cause| cause.downcast_ref::<std::io::Error>()
                .is_some_and(|io| matches!(io.kind(), std::io::ErrorKind::TimedOut | std::io::ErrorKind::WouldBlock))) => {
                return Err(error.context("Instance runtime is not responding"));
            }
            Err(_) => {}
        }
        if let Some(mut previous) = self.owned.remove(id) {
            let _ = previous.kill();
            let _ = previous.wait();
        }
        let log = File::options()
            .append(true)
            .create(true)
            .open(path.join("runtime.log"))?;
        let mut command = crate::process::command(&self.executable);
        command
            .args(["--root"])
            .arg(&self.catalog.root)
            .arg("--data-dir")
            .arg(&self.catalog.data)
            .arg("_runtime")
            .arg(id)
            .stdin(Stdio::null())
            .stdout(log.try_clone()?)
            .stderr(log);
        #[cfg(unix)]
        {
            use std::os::unix::process::CommandExt;
            command.process_group(0);
        }
        let child = command.spawn()?;
        self.owned.insert(id.into(), child);
        let deadline = Instant::now() + Duration::from_secs(10);
        loop {
            if let Ok(client) = Client::connect_for_control(path) {
                return Ok(client);
            }
            if let Some(status) = self.owned.get_mut(id).unwrap().try_wait()? {
                bail!(
                    "Runtime exited with {status}; see {}",
                    path.join("runtime.log").display()
                );
            }
            ensure!(Instant::now() < deadline, "Runtime startup timed out");
            thread::sleep(Duration::from_millis(20));
        }
    }
    pub fn disown(&mut self) {
        self.owned.clear();
        self.runs.clear();
    }
    pub fn stop_owned(&mut self) {
        for (id, run) in std::mem::take(&mut self.runs) {
            if let Ok(item) = self.catalog.device(&id)
                && let Ok(client) = Client::connect(Path::new(item["path"].as_str().unwrap()))
            {
                let _ = client.call("stop", json!({"run":run}));
            }
        }
        for (id, mut child) in std::mem::take(&mut self.owned) {
            if let Ok(item) = self.catalog.device(&id)
                && let Ok(client) = Client::connect(Path::new(item["path"].as_str().unwrap()))
            {
                // A different client may have started a newer run. Its
                // instance lifetime does not belong to this GUI/CLI owner.
                if client
                    .call("status", json!({}))
                    .ok()
                    .is_some_and(|s| s["session"]["finished"] == false)
                {
                    continue;
                }
                let _ = client.call("shutdown", json!({}));
            }
            let deadline = Instant::now() + Duration::from_secs(5);
            while child.try_wait().ok().flatten().is_none() && Instant::now() < deadline {
                thread::sleep(Duration::from_millis(10));
            }
            if child.try_wait().ok().flatten().is_none() {
                let _ = child.kill();
                let _ = child.wait();
            }
        }
    }
    pub fn status(&self) -> Result<Value> {
        let devices = self.catalog.devices()?;
        let mut sessions = json!({});
        let mut serial = json!({});
        let mut runtimes = json!({});
        let client_build = crate::identity::build();
        let mut session = Value::Null;
        let endpoints: Vec<_> = devices.iter().filter_map(|item| {
            if !item["unavailable"].is_null() { return None; }
            let id = item["id"].as_str()?;
            let path = Path::new(item["path"].as_str()?);
            path.join("runtime.json").exists().then_some((id, path))
        }).collect();
        // One bounded round per refresh, with fair rotation for larger libraries.
        // Cached observations preserve other devices while slow workers time out.
        let mut observed = self.observed.lock().unwrap();
        observed.values.retain(|id, _| endpoints.iter().any(|(key, _)| *key == id));
        observed.errors.retain(|id, _| endpoints.iter().any(|(key, _)| *key == id));
        let count = endpoints.len().min(OBSERVATION_CONCURRENCY);
        let start = observed.next;
        let observations = thread::scope(|scope| {
            let probes: Vec<_> = (0..count).map(|offset| {
                let (id, path) = endpoints[(start + offset) % endpoints.len()];
                (id, scope.spawn(move || Client::status_at(path)))
            }).collect();
            probes.into_iter().map(|(id, probe)| {
                (id, probe.join().unwrap_or_else(|_| Err(anyhow::anyhow!("Runtime observation failed"))))
            }).collect::<Vec<_>>()
        });
        observed.next = if endpoints.is_empty() { 0 } else { (start + count) % endpoints.len() };
        let sampled: Vec<_> = observations.iter().map(|(id, _)| *id).collect();
        for (id, result) in observations {
            match result {
                Ok(value) => {
                    observed.values.insert(id.to_owned(), value);
                    observed.errors.remove(id);
                }
                Err(error) => { observed.errors.insert(id.to_owned(), error.to_string()); }
            }
        }
        let errors = json!(observed.errors);
        for (id, _) in endpoints {
            let stale = !sampled.contains(&id) || observed.errors.contains_key(id);
            if let Some(value) = observed.values.get(id) {
                sessions[id] = value["session"].clone();
                serial[id] = value["serial"].clone();
                runtimes[id] = json!({"worker":value["worker"],
                    "client_worker_build":crate::identity::comparison(&client_build, &value["worker"]["build"]),
                    "client_qemu_binary":self.qemu_comparison(value),
                    "observation_error":errors[id], "stale":stale});
                if self.selected.as_deref() == Some(id)
                    || (session.is_null() && value["session"].is_object()) {
                    session = value["session"].clone();
                }
            } else {
                runtimes[id] = json!({"observation_error":errors[id],"stale":true});
            }
        }
        Ok(
            json!({"devices":devices,"boards":self.catalog.boards.values().collect::<Vec<_>>(),"chips":self.catalog.chips.values().collect::<Vec<_>>(),
            "sessions":sessions,"session":session,"serial":serial,"data_dir":self.catalog.data,
            "client_build":client_build,"runtimes":runtimes,"runtime_errors":errors,
            "available_qemu_sha256":self.available_qemu_sha256,
            "capabilities":{"host_camera":crate::camera_capture::SUPPORTED,"backend":"qemu","host_network":true,"audio_input":true,"audio_output":true,"microphone":true,"uart_rx":true,"serial_pty":cfg!(unix),"serial_tcp":cfg!(windows)}}),
        )
    }
    fn qemu_comparison(&self, runtime: &Value) -> &'static str {
        match (
            self.available_qemu_sha256.as_deref(),
            runtime["session"]["qemu"]["sha256"].as_str(),
        ) {
            (Some(a), Some(b)) if a == b => "same",
            (Some(_), Some(_)) => "different",
            _ => "unknown",
        }
    }
    pub fn call(&mut self, method: &str, params: Value) -> Result<Value> {
        if matches!(method, "status" | "create" | "start" | "board_import") {
            self.catalog.reload_hardware()?;
        }
        if method == "status" {
            return self.status();
        }
        if method == "camera_devices" {
            return crate::camera_capture::devices(&crate::assets::Assets::new(
                &self.catalog.root,
            )?);
        }
        if method == "create" {
            let package = params["package"].as_str().map(Path::new);
            return self.catalog.create(
                params["board"].as_str().context("Board is required")?,
                package,
                params["name"].as_str(),
            );
        }
        if method == "board_import" {
            return self.catalog.import_board(Path::new(
                params["path"].as_str().context("Board path is required")?));
        }
        if method == "board_validate" {
            return crate::board::compile(&crate::storage::read_json(Path::new(
                params["path"].as_str().context("Board path is required")?))?);
        }
        if method == "attach" {
            return self.catalog.attach(Path::new(
                params["path"].as_str().context("Instance path missing")?,
            ));
        }
        let id = params["id"]
            .as_str()
            .context("Instance ID is required")?
            .to_owned();
        let item = self.catalog.device(&id)?;
        let path = PathBuf::from(item["path"].as_str().unwrap());
        let layout = self.catalog.layout(item["board"].as_str().unwrap())?;
        self.selected = Some(id.clone());
        match method {
            "inspect" => {
                let runtime = Client::status_at(&path).unwrap_or(Value::Null);
                let client_build = crate::identity::build();
                Ok(
                    json!({"device":item,"runtime":runtime,"client_build":client_build,
                    "client_worker_build":crate::identity::comparison(&client_build, &runtime["worker"]["build"]),
                    "available_qemu_sha256":self.available_qemu_sha256,
                    "client_qemu_binary":self.qemu_comparison(&runtime)}),
                )
            }
            "start" => {
                let options = if let Some(options) = params.get("options") {
                    serde_json::from_value(options.clone())?
                } else {
                    Options {
                        seconds: params["seconds"].as_u64().unwrap_or(300),
                        timeout: params["timeout"].as_u64().unwrap_or(800),
                        network: params["online"]
                            .as_bool()
                            .unwrap_or(item["capabilities"]["wifi"] == true && item["host"]["online"].as_bool().unwrap_or(false)),
                        host_audio: item["capabilities"]["audio"] == true,
                        microphone: item["capabilities"]["audio"] == true && item["host"]["microphone"].as_bool().unwrap_or(false),
                        sound: item["host"]["sound"].as_bool().unwrap_or(true),
                        download: false,
                        capture: params["capture"].as_str().map(PathBuf::from),
                    }
                };
                let result = self
                    .worker(&id)?
                    .call("start", json!({"options":options}))?;
                self.runs.insert(id, result["session"]["output"].clone());
                self.status()
            }
            "stop"
            | "reset"
            | "reset_download"
            | "button"
            | "serial"
            | "uart_write"
            | "screenshot"
            | "audio"
            | "audio_cancel"
            | "audio_record"
            | "uart_read"
            | "signals"
            | "sd_card"
            | "usb_adb"
            | "events"
            | "uart_capture"
            | "button_sequence"
            | "button_sequence_status"
            | "button_sequence_cancel"
            | "touch"
            | "camera" => {
                let value = self.worker(&id)?.call(method, params)?;
                if ["reset", "reset_download"].contains(&method) {
                    self.runs.insert(id, value["session"]["output"].clone());
                }
                if ["stop", "reset", "reset_download"].contains(&method) {
                    self.status()
                } else {
                    Ok(value)
                }
            }
            "shutdown" => {
                let result = self.worker(&id)?.call("shutdown", json!({}));
                if let Some(mut child) = self.owned.remove(&id) {
                    let _ = child.wait();
                }
                result
            }
            "import" => {
                let package = Path::new(params["package"].as_str().context("LPK path missing")?);
                storage::import(&path, &layout, package)?;
                self.catalog.describe(&path)
            }
            "write_flash" => {
                storage::write_flash(
                    &path,
                    &layout,
                    Path::new(params["path"].as_str().context("Image path missing")?),
                    params["offset"].as_u64().unwrap_or(0).try_into()?,
                )?;
                self.catalog.describe(&path)
            }
            "erase" | "regenerate_uid" => {
                ensure!(
                    params["confirm_uid"] == item["uid"],
                    "Confirm the current instance UID"
                );
                if method == "erase" {
                    storage::erase(&path, &layout)?;
                } else {
                    storage::regenerate_uid(&path, &layout)?;
                }
                self.catalog.describe(&path)
            }
            "microphone" | "access_point" => {
                let setting = if method == "access_point" {
                    "wifi_ap_present"
                } else {
                    "microphone"
                };
                let enabled = params["enabled"]
                    .as_bool()
                    .context("Input selection requires a boolean")?;
                if let Ok(client) = Client::connect(&path) {
                    client.call(method, params)
                } else {
                    ensure!(
                        params["run"].is_null(),
                        "The request belongs to an earlier run"
                    );
                    let _storage = storage::locked(&path, &layout)?;
                    self.catalog.update(&path, &json!({setting:enabled}))?;
                    Ok(json!({"status":"saved","enabled":enabled}))
                }
            }
            "wifi_environment" => {
                let environment = params.get("environment").context("Missing Wi-Fi environment")?;
                crate::wifi_environment::WifiEnvironment::from_value(environment)?;
                if let Ok(client) = Client::connect(&path) {
                    let mut change = client.call(method, params.clone())?;
                    let request = change["request"].clone();
                    let deadline = Instant::now() + Duration::from_secs(30);
                    while change["status"] == "pending" && Instant::now() < deadline {
                        thread::sleep(Duration::from_millis(50));
                        let current = client.call("status", json!({}))?;
                        ensure!(current["session"]["output"] == params["run"], "The request belongs to an earlier run");
                        change = current["session"]["wifi_environment_change"].clone();
                        ensure!(change["request"] == request, "Wi-Fi environment confirmation was superseded");
                    }
                    ensure!(change["status"] == "applied" || change["status"] == "saved",
                        "Wi-Fi environment was not confirmed; check runtime status");
                    if let Some(error) = change["error"].as_str() { bail!("{error}"); }
                    self.catalog.describe(&path)
                } else {
                    ensure!(params["run"].is_null(), "The request belongs to an earlier run");
                    let _storage = storage::locked(&path, &layout)?;
                    self.catalog.update(&path, &json!({"wifi_environment":environment}))
                }
            }
            "settings" | "rename" | "sound" => {
                let changes = if method == "sound" {
                    json!({"sound":params["enabled"]})
                } else {
                    params.clone()
                };
                if let Some(environment) = changes.get("wifi_environment") {
                    ensure!(changes.as_object().unwrap().keys().all(|key|
                        ["id", "run", "wifi_environment"].contains(&key.as_str())),
                        "Change Wi-Fi environment separately");
                    return self.call("wifi_environment", json!({"id":id,"run":params["run"],"environment":environment}));
                }
                if let Some(present) = changes.get("wifi_ap_present") {
                    ensure!(
                        changes.as_object().unwrap().keys().all(|key| [
                            "id",
                            "run",
                            "wifi_ap_present"
                        ]
                        .contains(&key.as_str())),
                        "Change access point presence separately"
                    );
                    return self.call(
                        "access_point",
                        json!({"id":id,"run":params["run"],"enabled":present}),
                    );
                }
                if let Some(enabled) = changes.get("microphone")
                    && let Ok(client) = Client::connect(&path)
                    && client.call("status", json!({}))?["session"]["finished"] == false
                {
                    ensure!(
                        changes
                            .as_object()
                            .unwrap()
                            .keys()
                            .all(|key| ["id", "run", "microphone"].contains(&key.as_str())),
                        "Change microphone and other settings separately while running"
                    );
                    return client
                        .call("microphone", json!({"enabled":enabled,"run":params["run"]}));
                }
                let _storage = if changes.get("online").is_some()
                    || changes.get("microphone").is_some()
                    || changes.get("wifi_environment").is_some()
                {
                    Some(storage::locked(&path, &layout)?)
                } else {
                    None
                };
                let result = self.catalog.update(&path, &changes)?;
                if let Some(sound) = changes["sound"].as_bool()
                    && let Ok(client) = Client::connect(&path)
                {
                    client.call("mute", json!({"muted":!sound}))?;
                }
                Ok(result)
            }
            "detach" => {
                let _storage = storage::locked(&path, &layout)?;
                drop(_storage);
                if let Ok(client) = Client::connect(&path) {
                    client.call("shutdown", json!({}))?;
                }
                self.catalog.detach(&id)?;
                Ok(json!(true))
            }
            _ => bail!("Unknown management method"),
        }
    }
}
impl Drop for Manager {
    fn drop(&mut self) {
        self.stop_owned();
    }
}

pub fn bridge(mut manager: Manager) -> Result<()> {
    let result = serve_bridge(&mut manager);
    manager.disown();
    result
}

fn serve_bridge(manager: &mut Manager) -> Result<()> {
    manager.restore_ports()?;
    let stdin = std::io::stdin();
    let mut stdout = std::io::stdout().lock();
    for line in stdin.lock().lines() {
        let line = line?;
        ensure!(line.len() <= 65536, "Control request too large");
        let request: Value = serde_json::from_str(&line)?;
        let result = manager.call(
            request["method"].as_str().context("Missing method")?,
            request["params"].clone(),
        );
        let reply = match result {
            Ok(value) => json!({"id":request["id"],"result":value}),
            Err(error) => json!({"id":request["id"],"error":error.to_string()}),
        };
        writeln!(stdout, "{reply}")?;
        stdout.flush()?;
    }
    Ok(())
}
