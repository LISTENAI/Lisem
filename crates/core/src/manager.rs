//! Front-end-neutral management API. Each running instance has its own worker;
//! foreground CLI ownership and GUI ownership both use the same lifecycle.
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

pub struct Manager {
    pub catalog: Catalog,
    executable: PathBuf,
    owned: BTreeMap<String, Child>,
    runs: BTreeMap<String, Value>,
    selected: Option<String>,
}
impl Manager {
    pub fn new(root: &Path, data: &Path, executable: &Path) -> Result<Self> {
        Ok(Self {
            catalog: Catalog::open(root, data)?,
            executable: executable.to_owned(),
            owned: BTreeMap::new(),
            runs: BTreeMap::new(),
            selected: None,
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
        if let Ok(client) = Client::connect(path) {
            return Ok(client);
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
            if let Ok(client) = Client::connect(path) {
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
        let mut session = Value::Null;
        for item in &devices {
            if !item["unavailable"].is_null() {
                continue;
            }
            let id = item["id"].as_str().context("Missing ID")?;
            if let Ok(client) = Client::connect(Path::new(item["path"].as_str().unwrap())) {
                let value = client.call("status", json!({}))?;
                sessions[id] = value["session"].clone();
                serial[id] = value["serial"].clone();
                if self.selected.as_deref() == Some(id)
                    || (session.is_null() && value["session"].is_object())
                {
                    session = value["session"].clone();
                }
            }
        }
        Ok(
            json!({"devices":devices,"boards":self.catalog.boards.values().collect::<Vec<_>>(),"chips":self.catalog.chips.values().collect::<Vec<_>>(),
            "sessions":sessions,"session":session,"serial":serial,"data_dir":self.catalog.data,
            "capabilities":{"backend":"qemu","host_network":true,"audio_input":true,"audio_output":true,"microphone":true,"uart_rx":true,"serial_pty":cfg!(unix),"serial_tcp":cfg!(windows)}}),
        )
    }
    pub fn call(&mut self, method: &str, params: Value) -> Result<Value> {
        if method == "status" {
            return self.status();
        }
        if method == "create" {
            let package = params["package"].as_str().map(Path::new);
            return self.catalog.create(
                params["board"].as_str().context("Board is required")?,
                package,
                params["name"].as_str(),
            );
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
            "start" => {
                let options = if let Some(options) = params.get("options") {
                    serde_json::from_value(options.clone())?
                } else {
                    Options {
                        seconds: params["seconds"].as_u64().unwrap_or(300),
                        timeout: params["timeout"].as_u64().unwrap_or(800).min(850),
                        network: params["online"]
                            .as_bool()
                            .unwrap_or(item["host"]["online"].as_bool().unwrap_or(false)),
                        host_audio: true,
                        microphone: item["host"]["microphone"].as_bool().unwrap_or(false),
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
            "stop" | "reset" | "reset_download" | "button" | "serial" | "uart_write"
            | "screenshot" | "audio" => {
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
            "settings" | "rename" | "sound" => {
                let changes = if method == "sound" {
                    json!({"sound":params["enabled"]})
                } else {
                    params.clone()
                };
                let _storage =
                    if changes.get("online").is_some() || changes.get("microphone").is_some() {
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
