//! Runtime assets and backend selection. Platform and chip details stop here;
//! instance storage and front ends consume descriptors and capabilities.
use crate::{catalog::Catalog, storage};
use anyhow::{Context, Result, ensure};
use serde_json::Value;
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
            .join("lisa-camera")
    }
    pub fn verify_camera(&self) -> Result<()> {
        ensure!(
            cfg!(target_os = "macos"),
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
        walk(&self.root, &self.root.join("qemu"), &mut paths)?;
        paths.insert("tools/build_qemu.py".into());
        paths.insert("patches/qemu-n300.patch".into());
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
        if cfg!(target_os = "macos") {
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

/// Chip/board adapter for the currently compiled QEMU machine. Additional
/// chips register their launch and control ABI here; CPU/IP models remain QEMU's.
pub struct Backend {
    pub machine: String,
}
impl Backend {
    pub fn for_device(catalog: &Catalog, item: &Value) -> Result<Self> {
        catalog.check_hardware(&item["hardware"])?;
        ensure!(
            item["hardware"]["chip"]["id"] == "ls2684" && item["board"] == "arcs-mini",
            "No runtime backend registered for this chip and board"
        );
        Ok(Self {
            machine: "arcs-mini".into(),
        })
    }
    pub fn configure(
        &self,
        command: &mut Command,
        instance: &Path,
        output: &Path,
        seconds: u64,
        download: bool,
        capture: bool,
    ) {
        for (key, _) in std::env::vars_os() {
            if key.to_string_lossy().starts_with("ARCS_QEMU_") {
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
                "-bios",
            ])
            .arg(instance.join("flash.bin"))
            .env("ARCS_QEMU_BUDGET_NS", (seconds * 1_000_000_000).to_string())
            .env("ARCS_QEMU_BOOT_HART", "0")
            .env("ARCS_QEMU_LUNA_SAFE_READS", "1")
            .env("ARCS_QEMU_SOC_CLOCK", "10000")
            .env("ARCS_QEMU_PACE", "1")
            .env("ARCS_QEMU_FLASH_PERSIST", "1")
            .env("ARCS_QEMU_OTP_IMAGE", instance.join("otp.bin"))
            .env("ARCS_QEMU_DESKTOP", output.join("live"))
            .env("ARCS_QEMU_WIFI_AP", "Lisem");
        if capture {
            command
                .env("ARCS_QEMU_REPORT", output.join("report.json"))
                .env("ARCS_QEMU_SCREEN", output.join("screen.ppm"))
                .env("ARCS_QEMU_AUDIO_OUTPUT", output.join("audio.wav"))
                .env("ARCS_QEMU_WIFI_CAPTURE", output.join("wifi-tx.pcap"))
                .env("ARCS_QEMU_BLE_CAPTURE", output.join("ble-tx.jsonl"));
        } else {
            command.env("ARCS_QEMU_REPORT", "-");
        }
        if download {
            command.env("ARCS_QEMU_BOOT_RELEASE_NS", "50000000");
        }
    }
    pub fn button(&self, id: &str) -> Result<&'static str> {
        ensure!(id == "function", "Unknown board button");
        Ok("x-lisa-function-pressed")
    }
}
