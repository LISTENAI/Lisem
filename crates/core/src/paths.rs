//! Shared host paths. Branding changes must not strand existing instances.
use anyhow::{Context, Result, bail};
use std::path::PathBuf;

pub fn runtime_root(explicit: Option<PathBuf>) -> Result<PathBuf> {
    if let Some(path) = explicit.or_else(|| std::env::var_os("LISEM_ROOT").map(PathBuf::from)) {
        return Ok(path.canonicalize()?);
    }
    let executable = std::env::current_exe()?;
    if let Some(directory) = executable.parent() {
        let bundled = directory.join("runtime");
        if bundled.join("manifest.json").is_file() {
            return Ok(bundled);
        }
    }
    if let Some(contents) = executable.parent().and_then(|p| p.parent()) {
        let bundled = contents.join("Resources/runtime");
        if bundled.join("manifest.json").is_file() {
            return Ok(bundled);
        }
    }
    let current = std::env::current_dir()?;
    if current.join("boards").is_dir() && current.join("chips").is_dir() {
        return Ok(current);
    }
    bail!("Runtime resources missing; use an application bundle or --root")
}

pub fn data_dir() -> Result<PathBuf> {
    if let Some(value) =
        std::env::var_os("LISEM_DATA_DIR").or_else(|| std::env::var_os("LISA_SIM_DATA_DIR"))
    {
        return Ok(value.into());
    }
    #[cfg(target_os = "macos")]
    let base = PathBuf::from(std::env::var_os("HOME").context("HOME is missing")?)
        .join("Library/Application Support");
    #[cfg(target_os = "windows")]
    let base = PathBuf::from(std::env::var_os("LOCALAPPDATA").context("LOCALAPPDATA is missing")?);
    #[cfg(not(any(target_os = "macos", target_os = "windows")))]
    let base = match std::env::var_os("XDG_DATA_HOME") {
        Some(path) => PathBuf::from(path),
        None => {
            PathBuf::from(std::env::var_os("HOME").context("HOME is missing")?).join(".local/share")
        }
    };
    let (name, previous) = if cfg!(any(target_os = "macos", target_os = "windows")) {
        ("Lisem", "LISA Sim")
    } else {
        ("lisem", "lisa-sim")
    };
    let current = base.join(name);
    let previous = base.join(previous);
    Ok(
        if !current.exists() && previous.join("devices.json").is_file() {
            previous
        } else {
            current
        },
    )
}
