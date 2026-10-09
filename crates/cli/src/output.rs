use crate::Action;
use serde_json::Value;
use std::fmt::Write;

pub enum Output {
    List,
    Device,
    Status,
    Run(String),
    Text,
    Raw,
    Json,
    Message(String),
}
impl Output {
    pub fn for_action(action: &Action) -> Self {
        match action {
            Action::List => Self::List,
            Action::Create { .. } | Action::Attach { .. } => Self::Device,
            Action::Status { .. } => Self::Status,
            Action::Run { id, .. }
            | Action::Start { id, .. }
            | Action::Stop { id }
            | Action::Reset { id, .. } => Self::Run(id.clone()),
            Action::Uid { .. } | Action::Uart { disable: false, .. } => Self::Text,
            Action::Import { id, .. } => Self::Message(format!("Imported LPK into {id}.")),
            Action::Write { id, .. } => Self::Message(format!("Flash written: {id}.")),
            Action::Erase { id } => Self::Message(format!("Flash erased: {id}.")),
            Action::Button { button, state, .. } => {
                Self::Message(format!("Button {button}: {state}."))
            }
            Action::ButtonSequence { .. } | Action::ButtonSequenceStatus { .. } => Self::Json,
            Action::Screenshot { output, .. } => {
                Self::Message(format!("Screenshot saved: {}", output.display()))
            }
            Action::Uart { channel, .. } => Self::Message(format!("UART {channel} disabled.")),
            Action::Send { channel, .. } => Self::Message(format!("Sent to UART {channel}.")),
            Action::Shutdown { id } => Self::Message(format!("Runtime closed: {id}.")),
            Action::Logs { .. } => Self::Raw,
            Action::Runtime { .. } | Action::Bridge | Action::Mcp => unreachable!(),
        }
    }
    pub fn render(&self, value: &Value) -> String {
        match self {
            Self::Raw => unreachable!(),
            Self::Json => serde_json::to_string_pretty(value).unwrap(),
            Self::Text => value.as_str().unwrap_or_default().to_owned(),
            Self::Message(message) => message.clone(),
            Self::Device => device(value),
            Self::List => table(value, &Value::Null),
            Self::Status if value.get("device").is_some() => {
                let mut out = device(&value["device"]);
                let _ = write!(out, "\n{}", session(&value["runtime"]["session"]));
                if value["runtime"].is_object() {
                    let worker = &value["runtime"]["worker"];
                    let _ = write!(
                        out,
                        "\nWorker: {} (PID {}), client sources: {}",
                        text(&worker["build"]["version"]),
                        worker["pid"],
                        text(&value["client_worker_build"])
                    );
                    if let Some(digest) = value["runtime"]["session"]["qemu"]["sha256"].as_str() {
                        let _ = write!(out, "\nQEMU SHA256: {digest}");
                        let _ = write!(
                            out,
                            "\nQEMU matches client installation: {}",
                            text(&value["client_qemu_binary"])
                        );
                    }
                    if value["client_worker_build"] == "different"
                        || value["client_worker_build"] == "unknown"
                        || value["client_qemu_binary"] == "different"
                    {
                        out.push_str("\nCheck the worker build before testing changes. shutdown closes the runtime and UART endpoints; stop preserves the worker.");
                    }
                }
                if let Some(ports) = value["runtime"]["serial"].as_object() {
                    for (channel, endpoint) in ports {
                        let _ = write!(out, "\nUART {channel}: {}", text(endpoint));
                    }
                }
                out
            }
            Self::Status => table(&value["devices"], &value["sessions"]),
            Self::Run(id) => {
                let state = value["sessions"].get(id).unwrap_or(&value["session"]);
                format!("{id}\n{}", session(state))
            }
        }
    }
}
fn text(value: &Value) -> &str {
    value.as_str().unwrap_or("—")
}
fn device(value: &Value) -> String {
    format!(
        "{}\nID:    {}\nBoard: {}\nUID:   {}\nPath:  {}",
        text(&value["name"]),
        text(&value["id"]),
        text(&value["board"]),
        text(&value["uid"]),
        text(&value["path"])
    )
}
fn lifecycle(value: &Value) -> &'static str {
    if value["error"].is_string() || value["guest_fault"] == true {
        "Failed"
    } else if value["finished"] == false {
        "On"
    } else {
        "Off"
    }
}
fn session(value: &Value) -> String {
    let mut out = format!("Power: {}", lifecycle(value));
    if let Some(seconds) = value["seconds"].as_f64() {
        let _ = write!(out, "\nGuest time: {seconds:.2} s");
    }
    if let Some(seconds) = value["elapsed_seconds"].as_f64() {
        let _ = write!(out, "\nHost time: {seconds:.2} s");
    }
    if value["continuous_audio"] == true {
        let _ = write!(
            out,
            "\nAudio: {}",
            if value["microphone"] == true {
                "microphone + output"
            } else {
                "output"
            }
        );
    }
    if let Some(error) = value["error"].as_str() {
        let _ = write!(out, "\nError: {error}");
    }
    if value["guest_fault"] == true {
        out.push_str("\nGuest exception detected.");
    }
    out
}
fn table(devices: &Value, sessions: &Value) -> String {
    let Some(devices) = devices.as_array().filter(|v| !v.is_empty()) else {
        return "No instances. Create one with: lisem create --board arcs-mini".into();
    };
    let show_state = sessions.is_object();
    let mut out = if show_state {
        "ID                                POWER   BOARD       NAME"
    } else {
        "ID                                BOARD       NAME"
    }
    .to_owned();
    for item in devices {
        let id = text(&item["id"]);
        let _ = write!(out, "\n{id:32}  ");
        if show_state {
            let state = if item["unavailable"].is_string() {
                "Missing"
            } else {
                lifecycle(&sessions[id])
            };
            let _ = write!(out, "{state:6}  ");
        }
        let _ = write!(out, "{:<10}  {}", text(&item["board"]), text(&item["name"]));
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    #[test]
    fn human_output_preserves_identity_and_explains_failure_without_internal_dump() {
        let item = json!({"id":"full-instance-id", "name":"Mini", "board":"arcs-mini", "uid":"0123456789abcdef", "path":"/device"});
        let result = Output::Status.render(&json!({"device":item,"runtime":{"session":{"finished":true,"error":"UART closed","seconds":2.5,"report":{"internal":42}},"serial":{"0":"tcp://127.0.0.1:9000"}}}));
        assert!(result.contains("UID:   0123456789abcdef"));
        assert!(result.contains("Power: Failed\nGuest time: 2.50 s"));
        assert!(result.contains("Error: UART closed"));
        assert!(result.contains("UART 0: tcp://127.0.0.1:9000"));
        assert!(!result.contains("internal"));
        assert_eq!(
            Output::Text.render(&json!("0123456789abcdef")),
            "0123456789abcdef"
        );
        assert!(Output::List.render(&json!([])).starts_with("No instances."));
    }
}
