use anyhow::{Context, Result, ensure};
use serde_json::{Value, json};
use std::{fs, path::Path};

pub fn medium(value: &Value) -> Result<Value> {
    if value.is_null() {
        return Ok(Value::Null);
    }
    let path = Path::new(value["path"].as_str().context("SD image path missing")?);
    let path = fs::canonicalize(path)?;
    let metadata = fs::metadata(&path)?;
    let length = metadata.len();
    ensure!(
        metadata.is_file()
            && length.is_power_of_two()
            && (8 * 1024 * 1024..=32 * 1024 * 1024 * 1024).contains(&length),
        "SD image must be a raw file with a power-of-two size from 8 MiB to 32 GiB"
    );
    let read_only = value["read_only"].as_bool().unwrap_or(false);
    ensure!(!read_only, "Read-only SD media is not supported");
    if !read_only {
        fs::OpenOptions::new().write(true).open(&path)?;
    }
    Ok(json!({"path":path,"read_only":read_only,"bytes":length}))
}
