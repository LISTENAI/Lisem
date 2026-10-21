//! Runtime assets and backend selection. Platform and chip details stop here;
//! instance storage and front ends consume descriptors and capabilities.
use crate::{catalog::Catalog, storage};
use anyhow::{Context, Result, ensure};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeSet,
    fs,
    path::{Path, PathBuf},
    process::Command,
};

#[derive(Clone)]
pub struct Assets {
    pub root: PathBuf,
    bundled: bool,
}
impl Assets {
    pub fn new(root: &Path) -> Result<Self> {
        Ok(Self {
            root: root.canonicalize()?,
            bundled: root.join("manifest.json").is_file(),
        })
    }
    pub fn qemu(&self) -> PathBuf {
        self.root
            .join(if self.bundled {
                "bin"
            } else {
                ".tools/qemu-build"
            })
            .join(if cfg!(windows) {
                "qemu-system-riscv32.exe"
            } else {
                "qemu-system-riscv32"
            })
    }
    pub fn audio(&self) -> PathBuf {
        self.root
            .join(if self.bundled { "bin" } else { ".tools/audio" })
            .join(if cfg!(windows) {
                "lisa-audio.exe"
            } else {
                "lisa-audio"
            })
    }
    pub fn camera(&self) -> PathBuf {
        self.root
            .join(if self.bundled { "bin" } else { ".tools/camera" })
            .join(if cfg!(windows) { "lisa-camera.exe" } else { "lisa-camera" })
    }
    pub fn verify_camera(&self) -> Result<()> {
        ensure!(
            crate::camera_capture::SUPPORTED,
            "Host camera capture is unsupported on this platform"
        );
        if self.bundled {
            return self.verify_bundle();
        }
        let stamp = storage::read_json(&self.root.join(".tools/camera/build.json"))?;
        ensure!(
            stamp["binary_sha256"].as_str() == Some(&storage::sha256(&self.camera())?),
            "Host camera runtime changed; rebuild"
        );
        for (name, digest) in stamp["inputs"]
            .as_object()
            .context("Invalid camera build manifest")?
        {
            ensure!(
                digest.as_str() == Some(&storage::sha256(&self.root.join(name))?),
                "Host camera runtime is stale; rebuild"
            );
        }
        Ok(())
    }
    pub fn network(&self) -> PathBuf {
        self.root
            .join(if self.bundled {
                "lib"
            } else {
                ".tools/network"
            })
            .join(if cfg!(target_os = "macos") {
                "libarcs_slirp.dylib"
            } else if cfg!(windows) {
                "arcs_slirp.dll"
            } else {
                "libarcs_slirp.so"
            })
    }
    pub fn verify_qemu(&self) -> Result<()> {
        if self.bundled {
            return self.verify_bundle();
        }
        let stamp = storage::read_json(&self.root.join(".tools/qemu-build/arcs-build.json"))?;
        ensure!(
            stamp["binary_sha256"].as_str() == Some(&storage::sha256(&self.qemu())?),
            "QEMU runtime differs from its build manifest; rebuild the runtime"
        );
        let inputs = stamp["inputs_sha256"]
            .as_object()
            .context("Invalid QEMU build manifest")?;
        let mut paths = BTreeSet::new();
        fn walk(root: &Path, path: &Path, paths: &mut BTreeSet<String>) -> Result<()> {
            for entry in fs::read_dir(path)? {
                let path = entry?.path();
                if path.is_dir() {
                    walk(root, &path, paths)?;
                } else {
                    paths.insert(
                        path.strip_prefix(root)?
                            .to_string_lossy()
                            .replace('\\', "/"),
                    );
                }
            }
            Ok(())
        }
        walk(&self.root, &self.root.join("assets"), &mut paths)?;
        paths.insert("tools/build_qemu.py".into());
        let source = self.root.join("qemu").canonicalize()?;
        let prefix = String::from_utf8(source_git(&source, &["rev-parse", "--show-prefix"])?)?;
        ensure!(
            prefix.trim().is_empty(),
            "QEMU source is not an initialized submodule"
        );
        let revision = String::from_utf8(source_git(&source, &["rev-parse", "HEAD"])?)?;
        ensure!(
            stamp["qemu_revision"].as_str() == Some(revision.trim()),
            "QEMU revision changed; rebuild the runtime"
        );
        let mut digest = Sha256::new();
        digest.update(source_git(&source, &["diff", "--binary", "HEAD", "--"])?);
        let names = source_git(
            &source,
            &["ls-files", "--others", "--exclude-standard", "-z"],
        )?;
        let mut names: Vec<_> = names
            .split(|b| *b == 0)
            .filter(|name| !name.is_empty())
            .collect();
        names.sort();
        for name in names {
            #[cfg(unix)]
            let path = {
                use std::os::unix::ffi::OsStrExt;
                source.join(std::ffi::OsStr::from_bytes(name))
            };
            #[cfg(not(unix))]
            let path = source.join(std::str::from_utf8(name)?);
            digest.update(name);
            digest.update([0]);
            if path.is_symlink() {
                let target = fs::read_link(path)?;
                digest.update(
                    target
                        .to_str()
                        .context("QEMU symlink target is not UTF-8")?
                        .as_bytes(),
                );
            } else {
                digest.update(storage::sha256(&path)?.as_bytes());
            }
        }
        ensure!(
            stamp["qemu_worktree_sha256"].as_str() == Some(&storage::hex(&digest.finalize())),
            "QEMU source changed; rebuild the runtime"
        );
        ensure!(
            paths == inputs.keys().cloned().collect(),
            "QEMU source set changed; rebuild the runtime"
        );
        for (name, digest) in inputs {
            ensure!(
                digest.as_str() == Some(&storage::sha256(&self.root.join(name))?),
                "QEMU runtime differs from this checkout; run make build"
            );
        }
        Ok(())
    }
    pub fn verify_audio(&self) -> Result<()> {
        if self.bundled {
            return self.verify_bundle();
        }
        let stamp = storage::read_json(&self.root.join(".tools/audio/build.json"))?;
        ensure!(
            stamp["binary_sha256"].as_str() == Some(&storage::sha256(&self.audio())?),
            "Host audio runtime changed; rebuild"
        );
        for (name, digest) in stamp["inputs"]
            .as_object()
            .context("Invalid audio build manifest")?
        {
            ensure!(
                digest.as_str() == Some(&storage::sha256(&self.root.join(name))?),
                "Host audio runtime is stale; rebuild"
            );
        }
        Ok(())
    }

    fn verify_bundle(&self) -> Result<()> {
        let manifest = storage::read_json(&self.root.join("manifest.json"))?;
        ensure!(manifest["version"] == 1, "Unsupported runtime manifest");
        let files = manifest["files"]
            .as_object()
            .context("Runtime files missing")?;
        let mut assets = vec![self.qemu(), self.audio(), self.network()];
        if crate::camera_capture::SUPPORTED {
            assets.push(self.camera());
        }
        for asset in assets {
            let required = asset
                .strip_prefix(&self.root)?
                .to_str()
                .context("Invalid asset name")?
                .replace('\\', "/");
            ensure!(
                files.contains_key(&required),
                "Runtime asset missing: {required}"
            );
        }
        verify_files(&self.root, files)?;
        if !cfg!(target_os = "macos") {
            let host = manifest["host_files"]
                .as_object()
                .context("Host files missing")?;
            return verify_files(self.root.parent().context("Invalid bundle layout")?, host);
        }
        let frameworks = manifest["frameworks"]
            .as_object()
            .context("Runtime libraries missing")?;
        let directory = self
            .root
            .parent()
            .and_then(Path::parent)
            .context("Invalid application bundle layout")?
            .join("Frameworks")
            .canonicalize()?;
        verify_files(&directory, frameworks)
    }
}

fn verify_files(root: &Path, files: &serde_json::Map<String, Value>) -> Result<()> {
    for (name, digest) in files {
        let path = Path::new(name);
        ensure!(
            !path.is_absolute()
                && path
                    .components()
                    .all(|c| matches!(c, std::path::Component::Normal(_))),
            "Invalid runtime asset path"
        );
        let path = root.join(path).canonicalize()?;
        ensure!(path.starts_with(root), "Runtime asset escapes its bundle");
        ensure!(
            digest.as_str() == Some(&storage::sha256(&path)?),
            "Runtime asset differs from its manifest: {name}"
        );
    }
    Ok(())
}

fn source_git(source: &Path, arguments: &[&str]) -> Result<Vec<u8>> {
    use std::io::Read;
    use std::process::Stdio;
    use std::time::{Duration, Instant};
    const LIMIT: usize = 64 * 1024 * 1024;
    // The native cwd API accepts Windows verbatim paths; MSYS2 Git's -C does not.
    let mut child = crate::process::command("git")
        .current_dir(source)
        .args(arguments)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .context("Cannot inspect QEMU source; install Git and initialize the submodule")?;
    let output = child.stdout.take().unwrap();
    let reader = std::thread::spawn(move || {
        let mut bytes = Vec::new();
        output
            .take((LIMIT + 1) as u64)
            .read_to_end(&mut bytes)
            .map(|_| bytes)
    });
    let deadline = Instant::now() + Duration::from_secs(30);
    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => break Ok(status),
            Ok(None) if Instant::now() < deadline => std::thread::sleep(Duration::from_millis(10)),
            result => {
                let _ = child.kill();
                let _ = child.wait();
                break match result {
                    Err(error) => Err(anyhow::Error::from(error)),
                    _ => Err(anyhow::anyhow!("QEMU source inspection timed out")),
                };
            }
        }
    };
    let bytes = reader
        .join()
        .map_err(|_| anyhow::anyhow!("QEMU source reader failed"))??;
    ensure!(status?.success(), "Cannot inspect QEMU submodule with Git");
    ensure!(
        bytes.len() <= LIMIT,
        "QEMU source inspection exceeds 64 MiB"
    );
    Ok(bytes)
}

/// Hardware-specific launch and control ABI. Session owns transport and lifetime.
#[derive(Clone, Debug, serde::Serialize, serde::Deserialize, Default)]
#[serde(default, deny_unknown_fields)]
pub struct Capabilities {
    pub snapshot: bool,
    pub screen: bool,
    pub audio: bool,
    pub camera: bool,
    pub touch: bool,
    pub wifi: bool,
    pub sd: bool,
    pub usb_adb: bool,
    pub download: bool,
    pub signals: bool,
    pub signal_trace: bool,
    pub events: bool,
    pub button_sequence: bool,
    pub uart_channels: Vec<u8>,
}

pub struct Backend {
    pub machine: String,
    pub capabilities: Capabilities,
    pub environment_prefix: String,
    buttons: std::collections::BTreeMap<String, String>,
    sequence_ids: Vec<String>,
    touch_property: Option<String>,
    flash: String,
    clock_quantum_ns: Option<u64>,
    pub touch_size: Option<(u64, u64)>,
}
impl Backend {
    pub fn for_device(catalog: &Catalog, item: &Value) -> Result<Self> {
        let hardware = catalog.check_hardware(&item["hardware"])?;
        ensure!(
            hardware["board"]["runtime"]["available"] != false,
            "{}",
            hardware["board"]["runtime"]["unavailable_reason"]
                .as_str()
                .unwrap_or("This board runtime is unavailable")
        );
        Self::from_hardware(&hardware)
    }
    pub fn from_hardware(hardware: &Value) -> Result<Self> {
        let board = &hardware["board"];
        let chip = &hardware["chip"];
        let legacy = chip["id"] == "ls2684"
            && matches!(board["id"].as_str(), Some("arcs-mini" | "arcs-evb"));
        let runtime = &board["runtime"];
        let machine = runtime["machine"]
            .as_str()
            .or_else(|| legacy.then(|| board["id"].as_str()).flatten())
            .context("No runtime backend registered for this chip and board")?
            .to_owned();
        ensure!(
            !machine.is_empty()
                && machine
                    .bytes()
                    .all(|c| c.is_ascii_alphanumeric() || b"-_".contains(&c)),
            "Invalid QEMU machine name"
        );
        ensure!(
            legacy || chip["runtime"]["abi"] == "lisem-v1",
            "Unsupported chip runtime ABI"
        );
        let environment_prefix = chip["runtime"]["environment_prefix"]
            .as_str()
            .unwrap_or(if legacy { "ARCS_QEMU_" } else { "LISEM_QEMU_" })
            .to_owned();
        ensure!(
            !environment_prefix.is_empty()
                && environment_prefix
                    .bytes()
                    .all(|c| c.is_ascii_uppercase() || c == b'_'),
            "Invalid runtime environment prefix"
        );
        let mut capabilities: Capabilities = if runtime["capabilities"].is_null() {
            Capabilities {
                snapshot: legacy,
                wifi: legacy,
                sd: legacy,
                usb_adb: legacy,
                download: board["id"] == "arcs-mini",
                signals: legacy,
                events: legacy,
                button_sequence: legacy,
                ..Default::default()
            }
        } else {
            serde_json::from_value(runtime["capabilities"].clone())?
        };
        capabilities.screen =
            board["screen"].is_object() && runtime["capabilities"]["screen"] != false;
        capabilities.camera =
            board["camera"].is_object() && runtime["capabilities"]["camera"] != false;
        capabilities.audio = board["audio"] == true && runtime["capabilities"]["audio"] != false;
        capabilities.uart_channels = serde_json::from_value(board["uart_channels"].clone())
            .context("Board UART channels missing or invalid")?;
        ensure!(
            capabilities.uart_channels.len() <= 16
                && capabilities
                    .uart_channels
                    .iter()
                    .enumerate()
                    .all(|(i, n)| *n as usize == i),
            "Board UART channels must be consecutive from zero, at most 16"
        );
        let mut buttons = std::collections::BTreeMap::new();
        for button in board["buttons"].as_array().into_iter().flatten() {
            let id = button["id"].as_str().context("Button ID missing")?;
            let property = runtime["buttons"][id]
                .as_str()
                .map(str::to_owned)
                .or_else(|| {
                    if !legacy {
                        return None;
                    }
                    match (board["id"].as_str(), id) {
                        (Some("arcs-mini"), "function") => Some("x-lisa-function-pressed".into()),
                        (Some("arcs-evb"), "k1" | "k2" | "k3") => {
                            Some(format!("x-lisa-evb-{id}-pressed"))
                        }
                        _ => None,
                    }
                })
                .context("Board button has no runtime control")?;
            ensure!(
                !property.is_empty() && buttons.insert(id.to_owned(), property).is_none(),
                "Invalid or duplicate runtime button"
            );
        }
        let sequence_ids = if let Some(ids) = runtime["button_sequence_ids"].as_array() {
            ids.iter()
                .map(|v| {
                    v.as_str()
                        .map(str::to_owned)
                        .context("Invalid sequence button ID")
                })
                .collect::<Result<Vec<_>>>()?
        } else if legacy && board["id"] == "arcs-evb" {
            vec!["k1".into(), "k2".into(), "k3".into()]
        } else {
            Vec::new()
        };
        let unique: BTreeSet<_> = sequence_ids.iter().collect();
        ensure!(
            unique.len() == sequence_ids.len()
                && sequence_ids.iter().all(|id| buttons.contains_key(id)),
            "Invalid sequence button mapping"
        );
        ensure!(
            !capabilities.button_sequence || !sequence_ids.is_empty() || buttons.len() == 1,
            "Button sequences need an explicit ID mapping"
        );
        let touch_property = runtime["touch_property"]
            .as_str()
            .map(str::to_owned)
            .or_else(|| (legacy && board["touch"].is_object()).then(|| "x-lisa-touch".into()));
        let touch_size = if touch_property.is_some() {
            Some((
                board["touch"]["width"]
                    .as_u64()
                    .filter(|n| *n > 0)
                    .context("Touch width missing")?,
                board["touch"]["height"]
                    .as_u64()
                    .filter(|n| *n > 0)
                    .context("Touch height missing")?,
            ))
        } else {
            None
        };
        capabilities.touch = touch_property.is_some();
        let flash = chip["runtime"]["flash"]
            .as_str()
            .unwrap_or("bios")
            .to_owned();
        ensure!(
            ["bios", "pflash"].contains(&flash.as_str()),
            "Unsupported Flash attachment"
        );
        let clock_quantum_ns = if chip["runtime"]["clock_quantum_ns"].is_null() {
            legacy.then_some(10_000)
        } else {
            let quantum = chip["runtime"]["clock_quantum_ns"]
                .as_u64()
                .context("Invalid clock quantum")?;
            ensure!(quantum > 0, "Clock quantum must be positive");
            Some(quantum)
        };
        Ok(Self {
            machine,
            capabilities,
            environment_prefix,
            buttons,
            sequence_ids,
            touch_property,
            touch_size,
            flash,
            clock_quantum_ns,
        })
    }
    pub fn env(&self, suffix: &str) -> String {
        format!("{}{suffix}", self.environment_prefix)
    }
    pub fn configure(
        &self,
        command: &mut Command,
        instance: &Path,
        output: &Path,
        seconds: u64,
        download: bool,
        capture: bool,
    ) -> Result<()> {
        ensure!(
            !download || self.capabilities.download,
            "Download reset is unavailable on this board"
        );
        for (key, _) in std::env::vars_os() {
            if key.to_string_lossy().starts_with(&self.environment_prefix) {
                command.env_remove(key);
            }
        }
        command
            .args([
                "-M",
                &self.machine,
                "-accel",
                "tcg,thread=single",
                "-icount",
                "shift=0,align=off,sleep=off",
                "-display",
                "none",
                "-monitor",
                "none",
                "-S",
            ])
            .env(self.env("BUDGET_NS"), (seconds * 1_000_000_000).to_string())
            .env(self.env("PACE"), "1")
            .env(self.env("FLASH_PERSIST"), "1")
            .env(self.env("OTP_IMAGE"), instance.join("otp.bin"))
            .env(self.env("DESKTOP"), output.join("live"));
        if self.flash == "bios" {
            command.arg("-bios").arg(instance.join("flash.bin"));
        } else {
            let path = instance
                .join("flash.bin")
                .to_string_lossy()
                .replace(',', ",,");
            command.args(["-drive", &format!("if=pflash,format=raw,file={path}")]);
        }
        if self.environment_prefix == "ARCS_QEMU_" {
            command
                .env(self.env("BOOT_HART"), "0")
                .env(self.env("LUNA_SAFE_READS"), "1");
        }
        if let Some(quantum) = self.clock_quantum_ns {
            command.env(self.env("SOC_CLOCK"), quantum.to_string());
        }
        if self.capabilities.sd {
            command.args(["-drive", "if=none,id=sdcard"]);
        }
        if self.capabilities.wifi {
            command.env(self.env("WIFI_AP"), "Lisem");
        }
        if capture {
            command.env(self.env("REPORT"), output.join("report.json"));
            if self.capabilities.screen {
                command.env(self.env("SCREEN"), output.join("screen.ppm"));
            }
            if self.capabilities.audio {
                command.env(self.env("AUDIO_OUTPUT"), output.join("audio.wav"));
            }
            if self.capabilities.wifi {
                command.env(self.env("WIFI_CAPTURE"), output.join("wifi-tx.pcap"));
            }
            if self.environment_prefix == "ARCS_QEMU_" {
                command.env(self.env("BLE_CAPTURE"), output.join("ble-tx.jsonl"));
            }
        } else {
            command.env(self.env("REPORT"), "-");
        }
        if download {
            command.env(self.env("BOOT_RELEASE_NS"), "50000000");
        }
        Ok(())
    }
    pub fn button(&self, id: &str) -> Result<&str> {
        self.buttons
            .get(id)
            .map(String::as_str)
            .context("Unknown board button")
    }
    pub fn button_ids(&self) -> impl Iterator<Item = &str> {
        self.buttons.keys().map(String::as_str)
    }
    pub fn sequence_id(&self, id: &str) -> Result<Option<usize>> {
        ensure!(
            self.capabilities.button_sequence,
            "Button sequences are unavailable on this board"
        );
        self.button(id)?;
        if self.sequence_ids.is_empty() {
            Ok(None)
        } else {
            Ok(Some(
                self.sequence_ids
                    .iter()
                    .position(|v| v == id)
                    .context("Button has no sequence mapping")?,
            ))
        }
    }
    pub fn sequence_button(&self, id: Option<u64>) -> Option<&str> {
        if self.sequence_ids.is_empty() {
            self.buttons.keys().next().map(String::as_str)
        } else {
            self.sequence_ids
                .get(usize::try_from(id?).ok()?)
                .map(String::as_str)
        }
    }
    pub fn touch(&self) -> Result<&str> {
        self.touch_property
            .as_deref()
            .context("Touch input is unavailable on this board")
    }
}
