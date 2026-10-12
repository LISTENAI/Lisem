use anyhow::{Context, Result, ensure};
use lisem_core::manager::Manager;
use serde_json::{Value, json};
use std::{path::Path, sync::atomic::{AtomicBool, Ordering}, time::Duration};

pub fn receive(manager: &mut Manager, id: &str, channel: u8, path: &Path,
               timeout: u64, stop: &AtomicBool) -> Result<Value> {
    manager.call("serial", json!({"id":id,"channel":channel,"enabled":true}))?;
    let mut progress = manager.call("uart_capture", json!({"id":id,"channel":channel,
        "action":"start","path":path,"timeout":timeout}))?;
    let capture = progress["capture"].as_str().context("Missing capture identity")?.to_owned();
    loop {
        ensure!(progress["status"] != "failed", "UART receiver failed: {}", progress["error"]);
        if progress["status"] == "completed" { return Ok(progress); }
        std::thread::sleep(Duration::from_millis(50));
        progress = manager.call("uart_capture", json!({"id":id,"channel":channel,"capture":capture,
            "action": if stop.load(Ordering::Acquire) {"stop"} else {"status"}}))?;
    }
}
