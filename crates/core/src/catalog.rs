//! Hardware descriptions and the portable instance library.
use crate::storage::{self, Layout, MetadataLease};
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
        this.reload_hardware()?;
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

    pub fn reload_hardware(&mut self) -> Result<()> {
        let mut boards = BTreeMap::new();
        let mut chips = BTreeMap::new();
        for (directory, collection) in [("boards", &mut boards), ("chips", &mut chips)] {
            for path in fs::read_dir(self.root.join(directory))? {
                let path = path?.path();
                if path.extension().is_some_and(|s| s == "json") {
                    let mut value = storage::read_json(&path)?;
                    if directory == "boards" && value.get("schema_version").is_some() {
                        value = crate::board::compile(&value)
                            .with_context(|| format!("Invalid board descriptor: {}", path.display()))?;
                    }
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
        let custom = self.data.join("boards");
        if custom.is_dir() {
            for path in fs::read_dir(custom)? {
                let path = path?.path();
                if path.extension().is_some_and(|s| s == "json") {
                    let value = crate::board::compile(&storage::read_json(&path)?)?;
                    let id = value["id"].as_str().context("Board ID missing")?.to_owned();
                    ensure!(boards.insert(id, value).is_none(), "Custom board shadows an installed board");
                }
            }
        }
        for board in boards.values() { crate::signals::definitions(board)?; }
        self.boards = boards;
        self.chips = chips;
        Ok(())
    }

    pub fn import_board(&mut self, path: &Path) -> Result<Value> {
        let raw = storage::read_json(path)?;
        let board = crate::board::compile(&raw)?;
        let id = board["id"].as_str().unwrap();
        ensure!(!self.root.join("boards").join(format!("{id}.json")).exists(),
            "Installed board IDs cannot be replaced; choose a custom board ID");
        ensure!(self.chips.contains_key(board["chip"].as_str().unwrap()), "Unknown chip");
        let _index = MetadataLease::acquire(&self.data.join("library.lock"))?;
        self.reload_hardware()?;
        let mut leases = Vec::new();
        for entry in self.entries()? {
            let instance = Path::new(entry["path"].as_str().context("Instance path missing")?);
            let metadata = self.metadata(instance)?;
            if metadata["hardware"]["board"]["id"] == id {
                let old = self.layout(id)?;
                ensure!(old.flash_bytes as u64 == board["flash_bytes"]
                    && metadata["hardware"]["chip"]["id"] == board["chip"],
                    "Board update changes chip or persistent storage geometry");
                leases.push(storage::locked(instance, &old)?);
            }
        }
        let directory = self.data.join("boards");
        fs::create_dir_all(&directory)?;
        storage::write_json(&directory.join(format!("{id}.json")), &raw)?;
        self.boards.insert(id.to_owned(), board.clone());
        Ok(board)
    }

    pub fn layout(&self, board: &str) -> Result<Layout> {
        let board = self.boards.get(board).context("Unknown board")?;
        let chip = self
            .chips
            .get(board["chip"].as_str().context("Board chip missing")?)
            .context("Unknown chip")?;
        // Storage geometry belongs to the chip provider, not the UI/CLI.
        // All LS26 variants share the same OTP geometry and UID layout.
        let defaults = match chip["family"].as_str() {
            Some("arcs") => (512, 8, 8),
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
        let mut environment = value["environment"].clone();
        if environment.is_null() {
            environment = json!({});
        }
        environment["wifi"] = serde_json::to_value(
            crate::wifi_environment::WifiEnvironment::from_value(&value["environment"]["wifi"])?,
        )?;
        Ok(
            json!({"id":value["id"],"name":value["name"],"path":path,"board":layout.board,
                  "capabilities":crate::assets::Backend::from_hardware(&hardware)?.capabilities,
                  "hardware":hardware,"host":value["host"],"media":value["media"],
                  "environment":environment,"inputs":crate::signals::state(&hardware["board"], &value["inputs"])?,
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
        let _index = MetadataLease::acquire(&self.data.join("library.lock"))?;
        let mut entries = self.entries()?;
        let descriptor = self.boards.get(board).context("Unknown board")?;
        ensure!(descriptor["runtime"]["available"] != false, "{}",
            descriptor["runtime"]["unavailable_reason"].as_str().unwrap_or("This board runtime is unavailable"));
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
            "hardware":{"board":crate::board::source(&self.boards[board]),"chip":self.chips[self.boards[board]["chip"].as_str().unwrap()]},
            "host":{"online":true,"sound":true,"microphone":false,"uart":[]}});
        storage::write_json(&path.join("device.json"), &metadata)?;
        entries.push(json!({"id":id,"path":path}));
        self.save(&entries)?;
        self.describe(&path)
    }

    pub fn attach(&self, path: &Path) -> Result<Value> {
        let _index = MetadataLease::acquire(&self.data.join("library.lock"))?;
        let path = path.canonicalize()?;
        let mut entries = self.entries()?;
        if !path.join("device.json").exists() {
            let _meta = MetadataLease::acquire(&path.join("device.lock"))?;
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
                "hardware":{"board":crate::board::source(&self.boards[board]),"chip":self.chips[self.boards[board]["chip"].as_str().unwrap()]},
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
        let _index = MetadataLease::acquire(&self.data.join("library.lock"))?;
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
            value["host"]["wifi_ap_present"].is_null()
                || value["host"]["wifi_ap_present"].is_boolean(),
            "Invalid access point presence setting"
        );
        ensure!(
            value["host"]["microphone"].is_null() || value["host"]["microphone"].is_boolean(),
            "Invalid microphone setting"
        );
        ensure!(
            value["host"]["camera_image"].is_null() || value["host"]["camera_image"].is_string(),
            "Invalid camera image setting"
        );
        ensure!(
            value["host"]["camera_device"].is_null()
                || value["host"]["camera_device"]
                    .as_str()
                    .is_some_and(|id| !id.is_empty() && id.len() <= 4096),
            "Invalid host camera device setting"
        );
        ensure!(
            value["host"]["camera_image"].is_null() || value["host"]["camera_device"].is_null(),
            "Camera image and device are mutually exclusive"
        );
        ensure!(value["media"].is_null() || value["media"].is_object(), "Invalid media settings");
        ensure!(value["media"]["sd"].is_null() || (value["media"]["sd"]["path"].is_string()
            && value["media"]["sd"]["read_only"].is_boolean()), "Invalid SD image settings");
        if !value["host"]["uart"].is_null() {
            valid_uart(&value["host"]["uart"])?;
        }
        ensure!(
            value["environment"].is_null() || value["environment"].is_object(),
            "Invalid instance environment"
        );
        crate::wifi_environment::WifiEnvironment::from_value(&value["environment"]["wifi"])?;
        Ok(value)
    }

    pub fn update(&self, path: &Path, changes: &Value) -> Result<Value> {
        let _lease = MetadataLease::acquire(&path.join("device.lock"))?;
        let mut value = self.metadata(path)?;
        value.as_object_mut().unwrap().remove("firmware");
        if let Some(name) = changes.get("name") {
            value["name"] = json!(valid_name(name.as_str().context("Invalid name")?)?);
        }
        for key in ["online", "sound", "microphone", "wifi_ap_present"] {
            if let Some(setting) = changes.get(key) {
                ensure!(setting.is_boolean(), "Host setting must be boolean");
                value["host"][key] = setting.clone();
            }
        }
        if let Some(inputs) = changes.get("inputs") {
            let hardware = self.check_hardware(&value["hardware"])?;
            value["inputs"] = serde_json::to_value(crate::signals::overrides(&hardware["board"], inputs)?)?;
        }
        if let Some(medium) = changes.get("sd_card") {
            if value["media"].is_null() { value["media"] = json!({}); }
            value["media"]["sd"] = crate::sd_card::medium(medium)?;
        }
        if let Some(image) = changes.get("camera_image") {
            ensure!(
                image.is_null() || image.is_string(),
                "Invalid camera image setting"
            );
            value["host"]["camera_image"] = image.clone();
        }
        if let Some(device) = changes.get("camera_device") {
            ensure!(
                device.is_null()
                    || device
                        .as_str()
                        .is_some_and(|id| !id.is_empty() && id.len() <= 4096),
                "Invalid host camera device setting"
            );
            value["host"]["camera_device"] = device.clone();
        }
        ensure!(
            value["host"]["camera_image"].is_null() || value["host"]["camera_device"].is_null(),
            "Camera image and device are mutually exclusive"
        );
        if let Some(uart) = changes.get("uart") {
            valid_uart(uart)?;
            let backend = crate::assets::Backend::from_hardware(&self.check_hardware(&value["hardware"])?)?;
            ensure!(uart.as_array().unwrap().iter().all(|v| backend.capabilities.uart_channels.contains(&(v.as_u64().unwrap() as u8))), "UART channel is unavailable on this board");
            value["host"]["uart"] = uart.clone();
        }
        if let Some(environment) = changes.get("wifi_environment") {
            let wifi = crate::wifi_environment::WifiEnvironment::from_value(environment)?;
            if value["environment"].is_null() {
                value["environment"] = json!({});
            }
            value["environment"]["wifi"] = serde_json::to_value(wifi)?;
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
        if board.get("definition").is_some() {
            let previous = crate::board::source(&saved["board"]);
            let previous_flash = previous["storage"]["flash_bytes"].as_u64()
                .or_else(|| previous["flash_bytes"].as_u64());
            ensure!(previous["chip"] == board["chip"] && saved["board"]["chip"] == board["chip"]
                && saved["chip"]["id"] == chip["id"] && saved["chip"]["family"] == chip["family"]
                && previous_flash == board["flash_bytes"].as_u64()
                && (saved["board"]["flash_bytes"].is_null() || saved["board"]["flash_bytes"] == board["flash_bytes"]),
                "Board chip or persistent storage geometry changed");
            for (field, default) in [("otp_bytes",512), ("uid_offset",8), ("uid_bytes",8)] {
                let fallback = if chip["family"] == "arcs" { Some(default) } else { None };
                ensure!(saved["chip"]["storage"][field].as_u64().or(fallback)
                    == chip["storage"][field].as_u64().or(fallback),
                    "Chip persistent storage geometry changed");
            }
            return Ok(json!({"board":board,"chip":chip}));
        }
        saved["chip"]
            .as_object_mut()
            .context("Invalid chip")?
            .remove("backend");
        if saved["chip"]["id"] == "ls2684" && saved["chip"]["memory"].is_null() {
            saved["chip"]["memory"] = chip["memory"].clone();
        }
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
        // Older camera descriptors predate the board mounting direction.
        if saved["board"]["id"] == "arcs-mini" {
            if let Some(camera) = saved["board"]["camera"].as_object_mut() {
                camera
                    .entry("rotation_clockwise")
                    .or_insert_with(|| board["camera"]["rotation_clockwise"].clone());
            }
        }
        if saved["board"]["id"] == "arcs-evb" {
            if let Some(touch) = saved["board"]["touch"].as_object_mut() {
                touch
                    .entry("rotation_clockwise")
                    .or_insert_with(|| board["touch"]["rotation_clockwise"].clone());
            }
        }
        if saved["board"]["inputs"].is_null() && !board["inputs"].is_null() {
            saved["board"]["inputs"] = board["inputs"].clone();
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
            // Runtime controls and scheduling describe this emulator build,
            // not the instance's physical hardware or persistent storage.
            object.remove("runtime");
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
    for field in ["buttons", "inputs"] {
        if let Some(items) = value["board"][field].as_array_mut() {
            for item in items {
                if let Some(object) = item.as_object_mut() {
                    object.remove("label");
                    object.remove("description");
                }
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
            n < 16 && !seen.contains(&n),
            "UART channels must be unique integers in 0..15"
        );
        seen.push(n);
    }
    Ok(())
}
