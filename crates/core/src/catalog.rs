//! Hardware descriptions and the portable instance library.
use crate::storage::{self, Layout, Lease};
use anyhow::{Context, Result, ensure};
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    fs,
    path::{Path, PathBuf},
};
use uuid::Uuid;

pub struct Catalog {
    pub root: PathBuf,
    pub data: PathBuf,
    pub boards: BTreeMap<String, Value>,
    pub chips: BTreeMap<String, Value>,
}

impl Catalog {
    pub fn open(root: &Path, data: &Path) -> Result<Self> {
        fs::create_dir_all(data)?;
        let mut this = Self {
            root: root.canonicalize()?,
            data: data.canonicalize()?,
            boards: BTreeMap::new(),
            chips: BTreeMap::new(),
        };
        for (directory, collection) in [("boards", &mut this.boards), ("chips", &mut this.chips)] {
            for path in fs::read_dir(this.root.join(directory))? {
                let path = path?.path();
                if path.extension().is_some_and(|s| s == "json") {
                    let value = storage::read_json(&path)?;
                    let id = value["id"]
                        .as_str()
                        .context("Hardware descriptor has no ID")?
                        .to_owned();
                    ensure!(
                        collection.insert(id, value).is_none(),
                        "Duplicate hardware descriptor"
                    );
                }
            }
        }
        // Older libraries kept display metadata in their index. Upgrade only
        // that metadata; Flash, OTP and the storage manifest stay untouched.
        for entry in this.entries()? {
            let path = Path::new(entry["path"].as_str().context("Instance path missing")?);
            if path.join("instance.json").is_file() && !path.join("device.json").exists() {
                this.attach(path)?;
            }
        }
        Ok(this)
    }

    pub fn layout(&self, board: &str) -> Result<Layout> {
        let board = self.boards.get(board).context("Unknown board")?;
        let chip = self
            .chips
            .get(board["chip"].as_str().context("Board chip missing")?)
            .context("Unknown chip")?;
        // Storage geometry belongs to the chip provider, not the UI/CLI.
        // Older LS2684 descriptors omit the OTP fields; preserve their bytes.
        let defaults = match chip["id"].as_str() {
            Some("ls2684") => (512, 8, 8),
            _ => (0, 0, 0),
        };
        let layout = Layout {
            chip: chip["family"]
                .as_str()
                .context("Chip family missing")?
                .into(),
            board: board["id"].as_str().unwrap().into(),
            flash_bytes: usize::try_from(
                board["flash_bytes"]
                    .as_u64()
                    .context("Board Flash size missing")?,
            )?,
            otp_bytes: chip["storage"]["otp_bytes"].as_u64().unwrap_or(defaults.0) as usize,
            uid_offset: chip["storage"]["uid_offset"].as_u64().unwrap_or(defaults.1) as usize,
            uid_bytes: chip["storage"]["uid_bytes"].as_u64().unwrap_or(defaults.2) as usize,
        };
        ensure!(
            layout.flash_bytes > 0
                && layout.flash_bytes <= 1024 * 1024 * 1024
                && layout.otp_bytes > 0
                && layout.uid_bytes > 0
                && layout.uid_bytes <= 16
                && layout.uid_offset + layout.uid_bytes <= layout.otp_bytes,
            "Unsupported chip storage geometry"
        );
        Ok(layout)
    }

    pub fn entries(&self) -> Result<Vec<Value>> {
        let path = self.data.join("devices.json");
        if !path.exists() {
            return Ok(Vec::new());
        }
        let index = storage::read_json(&path)?;
        ensure!(
            index["version"] == 1 || index["version"] == 2,
            "Unsupported library index"
        );
        Ok(index["devices"]
            .as_array()
            .context("Malformed device library")?
            .clone())
    }

    fn save(&self, entries: &[Value]) -> Result<()> {
        let index: Vec<_> = entries
            .iter()
            .map(|v| json!({"id":v["id"],"path":v["path"]}))
            .collect();
        storage::write_json(
            &self.data.join("devices.json"),
            &json!({"version":2,"devices":index}),
        )
    }

    pub fn describe(&self, path: &Path) -> Result<Value> {
        let path = path.canonicalize()?;
        let value = self.metadata(&path)?;
        let layout = self.layout(
            value["hardware"]["board"]["id"]
                .as_str()
                .context("Board missing")?,
        )?;
        let hardware = self.check_hardware(&value["hardware"])?;
        Ok(
            json!({"id":value["id"],"name":value["name"],"path":path,"board":layout.board,
                  "hardware":hardware,"host":value["host"],
                  "uid":storage::uid(&path,&layout)?}),
        )
    }

    pub fn device(&self, id: &str) -> Result<Value> {
        let entries = self.entries()?;
        let entry = entries
            .iter()
            .find(|v| v["id"].as_str() == Some(id))
            .context("Unknown instance ID")?;
        let result = self.describe(Path::new(
            entry["path"].as_str().context("Instance path missing")?,
        ))?;
        ensure!(
            result["id"] == id,
            "Instance management ID changed outside this library"
        );
        Ok(result)
    }

    pub fn devices(&self) -> Result<Vec<Value>> {
        self.entries()?
            .iter()
            .map(|entry| {
                let path = Path::new(entry["path"].as_str().context("Instance path missing")?);
                Ok(self.describe(path).unwrap_or_else(|error| {
                    json!({
                        "id": entry["id"],
                        "path": path,
                        "name": path.file_name().unwrap_or_default().to_string_lossy(),
                        "board": "",
                        "uid": "",
                        "unavailable": error.to_string(),
                    })
                }))
            })
            .collect()
    }

    pub fn create(&self, board: &str, package: Option<&Path>, name: Option<&str>) -> Result<Value> {
        let _index = Lease::acquire(&self.data.join("library.lock"))?;
        let mut entries = self.entries()?;
        let layout = self.layout(board)?;
        let id = Uuid::new_v4().simple().to_string();
        let path = self.data.join("devices").join(&id);
        let mut count = 1;
        let names: Vec<_> = self
            .devices()?
            .iter()
            .filter_map(|v| v["name"].as_str().map(str::to_owned))
            .collect();
        let label = self.boards[board]["name"].as_str().unwrap_or(board);
        while names.contains(&format!("{label} {count}")) {
            count += 1;
        }
        let name = valid_name(name.unwrap_or(&format!("{label} {count}")))?;
        storage::create(&path, &layout, package)?;
        let metadata = json!({"version":1,"id":id,"name":name,
            "hardware":{"board":self.boards[board],"chip":self.chips[self.boards[board]["chip"].as_str().unwrap()]},
            "host":{"online":true,"sound":true,"microphone":false,"uart":[]}});
        storage::write_json(&path.join("device.json"), &metadata)?;
        entries.push(json!({"id":id,"path":path}));
        self.save(&entries)?;
        self.describe(&path)
    }

    pub fn attach(&self, path: &Path) -> Result<Value> {
        let _index = Lease::acquire(&self.data.join("library.lock"))?;
        let path = path.canonicalize()?;
        let mut entries = self.entries()?;
        if !path.join("device.json").exists() {
            let _meta = Lease::acquire(&path.join("device.lock"))?;
            let manifest = storage::read_json(&path.join("instance.json"))?;
            let board = manifest["board"].as_str().context("Board missing")?;
            let _storage = storage::locked(&path, &self.layout(board)?)?;
            let legacy = entries.iter().find(|v| {
                v["path"]
                    .as_str()
                    .and_then(|p| Path::new(p).canonicalize().ok())
                    .as_ref()
                    == Some(&path)
            });
            let id = legacy
                .and_then(|v| v["id"].as_str())
                .map(str::to_owned)
                .unwrap_or_else(|| Uuid::new_v4().simple().to_string());
            let name = legacy
                .and_then(|v| v["name"].as_str())
                .map(str::to_owned)
                .unwrap_or_else(|| path.file_name().unwrap().to_string_lossy().into_owned());
            storage::write_json(
                &path.join("device.json"),
                &json!({"version":1,"id":id,"name":valid_name(&name)?,
                "hardware":{"board":self.boards[board],"chip":self.chips[self.boards[board]["chip"].as_str().unwrap()]},
                "host":{"online":true,"sound":true}}),
            )?;
        }
        let item = self.describe(&path)?;
        for entry in &mut entries {
            if entry["id"] == item["id"] {
                let old = Path::new(entry["path"].as_str().context("Instance path missing")?);
                ensure!(
                    old.canonicalize().ok().as_ref() == Some(&path) || !old.exists(),
                    "This identity already exists at another path"
                );
                entry["path"] = json!(path);
                self.save(&entries)?;
                return Ok(item);
            }
            if let Ok(other) = self.describe(Path::new(
                entry["path"].as_str().context("Instance path missing")?,
            )) {
                ensure!(
                    other["uid"] != item["uid"],
                    "Another instance has the same chip UID"
                );
            }
        }
        entries.push(item.clone());
        self.save(&entries)?;
        Ok(item)
    }

    pub fn detach(&self, id: &str) -> Result<()> {
        let _index = Lease::acquire(&self.data.join("library.lock"))?;
        let item = self.device(id)?;
        let _storage = storage::locked(
            Path::new(item["path"].as_str().unwrap()),
            &self.layout(item["board"].as_str().unwrap())?,
        )?;
        let mut entries = self.entries()?;
        entries.retain(|v| v["id"] != id);
        self.save(&entries)
    }

    pub fn metadata(&self, path: &Path) -> Result<Value> {
        let value = storage::read_json(&path.join("device.json"))?;
        ensure!(value["version"] == 1, "Unsupported instance metadata");
        let id = value["id"].as_str().context("Missing instance ID")?;
        ensure!(
            Uuid::parse_str(id)?.simple().to_string() == id,
            "Invalid instance ID"
        );
        valid_name(value["name"].as_str().context("Missing instance name")?)?;
        for key in ["online", "sound"] {
            ensure!(value["host"][key].is_boolean(), "Invalid host setting");
        }
        ensure!(
            value["host"]["microphone"].is_null() || value["host"]["microphone"].is_boolean(),
            "Invalid microphone setting"
        );
        ensure!(
            value["host"]["camera_image"].is_null() || value["host"]["camera_image"].is_string(),
            "Invalid camera image setting"
        );
        if !value["host"]["uart"].is_null() {
            valid_uart(&value["host"]["uart"])?;
        }
        Ok(value)
    }

    pub fn update(&self, path: &Path, changes: &Value) -> Result<Value> {
        let _lease = Lease::acquire(&path.join("device.lock"))?;
        let mut value = self.metadata(path)?;
        value.as_object_mut().unwrap().remove("firmware");
        if let Some(name) = changes.get("name") {
            value["name"] = json!(valid_name(name.as_str().context("Invalid name")?)?);
        }
        for key in ["online", "sound", "microphone"] {
            if let Some(setting) = changes.get(key) {
                ensure!(setting.is_boolean(), "Host setting must be boolean");
                value["host"][key] = setting.clone();
            }
        }
        if let Some(image) = changes.get("camera_image") {
            ensure!(
                image.is_null() || image.is_string(),
                "Invalid camera image setting"
            );
            value["host"]["camera_image"] = image.clone();
        }
        if let Some(uart) = changes.get("uart") {
            valid_uart(uart)?;
            value["host"]["uart"] = uart.clone();
        }
        storage::write_json(&path.join("device.json"), &value)?;
        self.describe(path)
    }

    pub fn check_hardware(&self, hardware: &Value) -> Result<Value> {
        let mut saved = hardware.clone();
        let id = saved["board"]["id"]
            .as_str()
            .context("Missing board")?
            .to_owned();
        let board = self.boards.get(&id).context("Board not supported")?;
        let chip = self
            .chips
            .get(board["chip"].as_str().context("Missing chip")?)
            .context("Chip not supported")?;
        saved["chip"]
            .as_object_mut()
            .context("Invalid chip")?
            .remove("backend");
        if saved["platform_sha256"]
            == "ffd254848b5d290ea549c9dfb665137a89ea87a4d8fd8bc621d614b93c0a8ca5"
            && saved["board"]["platform"] == "platforms/arcs-mini.repl"
        {
            saved.as_object_mut().unwrap().remove("platform_sha256");
            saved["board"].as_object_mut().unwrap().remove("platform");
            if let Some(buttons) = saved["board"]["buttons"].as_array_mut() {
                for button in buttons {
                    if button["peripheral"] == "sysbus.gpioB" {
                        button.as_object_mut().unwrap().remove("peripheral");
                        button["bank"] = json!("B");
                    }
                }
            }
        }
        // Older Mini descriptors omitted its physically present camera.
        if saved["board"]["id"] == "arcs-mini" && saved["board"]["camera"].is_null() {
            saved["board"]["camera"] = board["camera"].clone();
        }
        let expected = json!({"board":board,"chip":chip});
        let saved = hardware_identity(saved);
        let expected_identity = hardware_identity(expected.clone());
        ensure!(
            saved == expected_identity,
            "Saved board revision differs from this runtime"
        );
        Ok(expected)
    }
}

fn hardware_identity(mut value: Value) -> Value {
    for section in ["board", "chip"] {
        if let Some(object) = value[section].as_object_mut() {
            object.remove("name");
        }
    }
    if let Some(screen) = value["board"]["screen"].as_object_mut() {
        screen.remove("label");
    }
    if let Some(camera) = value["board"]["camera"].as_object_mut() {
        camera.remove("label");
    }
    // Indicators observe signals; they do not add devices or change wiring.
    if let Some(board) = value["board"].as_object_mut() {
        board.remove("indicators");
    }
    if let Some(items) = value["board"]["buttons"].as_array_mut() {
        for item in items {
            if let Some(object) = item.as_object_mut() {
                object.remove("label");
            }
        }
    }
    value
}

pub fn valid_name(value: &str) -> Result<String> {
    let trimmed = value.trim();
    ensure!(
        (1..=80).contains(&trimmed.chars().count()) && !value.chars().any(|c| c < ' '),
        "Instance name must contain 1..80 characters without control characters"
    );
    Ok(trimmed.into())
}

pub fn valid_uart(value: &Value) -> Result<()> {
    let channels = value.as_array().context("UART channels must be an array")?;
    let mut seen = Vec::new();
    for channel in channels {
        let n = channel.as_u64().context("Invalid UART channel")?;
        ensure!(
            n < 3 && !seen.contains(&n),
            "UART channels must be unique integers in 0..2"
        );
        seen.push(n);
    }
    Ok(())
}
