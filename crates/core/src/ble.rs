//! Generic ATT client codec. Transport must return the peer's actual response.
use anyhow::{Context, Result, bail, ensure};
use serde_json::{Value, json};
use std::{
    collections::VecDeque,
    time::{Duration, Instant},
};
use uuid::Uuid;

#[derive(Debug)]
struct AttFailure {
    opcode: u8,
    handle: u16,
    code: u8,
}

impl std::fmt::Display for AttFailure {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "ATT error opcode=0x{:02x} handle=0x{:04x} code=0x{:02x}",
            self.opcode, self.handle, self.code
        )
    }
}

impl std::error::Error for AttFailure {}

pub fn decode_hex(value: &str, maximum: usize) -> Result<Vec<u8>> {
    ensure!(
        value.len() % 2 == 0 && value.len() <= maximum * 2,
        "Invalid BLE binary length"
    );
    value
        .as_bytes()
        .chunks(2)
        .map(|p| {
            let s = std::str::from_utf8(p)?;
            Ok(u8::from_str_radix(s, 16).context("Invalid BLE binary value")?)
        })
        .collect()
}

pub fn hex(value: &[u8]) -> String {
    value.iter().map(|b| format!("{b:02x}")).collect()
}

fn u16_at(data: &[u8], offset: usize) -> Result<u16> {
    let bytes = data
        .get(offset..offset + 2)
        .context("Truncated ATT field")?;
    Ok(u16::from_le_bytes([bytes[0], bytes[1]]))
}

fn uuid(value: &[u8]) -> Result<String> {
    ensure!(matches!(value.len(), 2 | 16), "Invalid ATT UUID length");
    let text = hex(&value.iter().rev().copied().collect::<Vec<_>>());
    Ok(if value.len() == 2 {
        text
    } else {
        format!(
            "{}-{}-{}-{}-{}",
            &text[..8],
            &text[8..12],
            &text[12..16],
            &text[16..20],
            &text[20..]
        )
    })
}

/// No service UUID, handle, or application protocol is built into this client.
pub fn execute(
    request: &Value,
    mut exchange: impl FnMut(&[u8]) -> Result<Vec<u8>>,
) -> Result<Value> {
    let action = request["action"].as_str().context("Missing GATT action")?;
    let handle = || -> Result<u16> {
        let h = request["handle"]
            .as_u64()
            .context("Missing attribute handle")?;
        ensure!(h > 0 && h <= 65535, "Invalid attribute handle");
        Ok(h as u16)
    };
    let checked = |sent: &[u8], received: Vec<u8>, expected: u8| -> Result<Vec<u8>> {
        ensure!(!received.is_empty(), "Empty ATT response");
        if received[0] == 1 {
            ensure!(
                received.len() == 5 && received[1] == sent[0],
                "Invalid ATT error response"
            );
            return Err(AttFailure {
                opcode: received[1],
                handle: u16_at(&received, 2)?,
                code: received[4],
            }
            .into());
        }
        ensure!(received[0] == expected, "Unexpected ATT response opcode");
        Ok(received)
    };
    match action {
        "read" => {
            let h = handle()?;
            let mut value = Vec::new();
            loop {
                let mut sent = vec![if value.is_empty() { 0x0a } else { 0x0c }];
                sent.extend(h.to_le_bytes());
                if sent[0] == 0x0c {
                    sent.extend((value.len() as u16).to_le_bytes());
                }
                let response = exchange(&sent)?;
                // An exact-multiple long value may end with Invalid Offset.
                if sent[0] == 0x0c
                    && response.len() == 5
                    && response[..4] == [1, 0x0c, h as u8, (h >> 8) as u8]
                    && (response[4] == 7 || (value.len() == 22 && response[4] == 0x0b))
                {
                    break;
                }
                let response = checked(&sent, response, sent[0] + 1)?;
                ensure!(response.len() <= 23, "ATT response exceeds negotiated MTU");
                value.extend_from_slice(&response[1..]);
                ensure!(value.len() <= 512, "ATT attribute exceeds 512 bytes");
                if response.len() < 23 {
                    break;
                }
            }
            Ok(json!({
                "status": "response",
                "handle": h,
                "value": hex(&value)
            }))
        }
        "write" | "subscribe" => {
            let h = handle()?;
            let value = decode_hex(
                request["value"].as_str().context("Missing binary value")?,
                512,
            )?;
            if request["response"] == false {
                ensure!(value.len() <= 20, "Write command exceeds negotiated MTU");
                let mut sent = vec![0x52];
                sent.extend(h.to_le_bytes());
                sent.extend(value);
                ensure!(
                    exchange(&sent)?.is_empty(),
                    "Unexpected write command response"
                );
                return Ok(json!({
                    "status": "delivered",
                    "handle": h
                }));
            }
            if value.len() > 20 {
                for (index, part) in value.chunks(18).enumerate() {
                    let mut sent = vec![0x16];
                    sent.extend(h.to_le_bytes());
                    sent.extend(((index * 18) as u16).to_le_bytes());
                    sent.extend(part);
                    let response = checked(&sent, exchange(&sent)?, 0x17)?;
                    ensure!(response[1..] == sent[1..], "ATT prepare-write echo differs");
                }
                let response = checked(&[0x18, 1], exchange(&[0x18, 1])?, 0x19)?;
                ensure!(response.len() == 1, "Invalid ATT execute-write response");
                return Ok(json!({
                    "status": "response",
                    "handle": h
                }));
            }
            let mut sent = vec![0x12];
            sent.extend(h.to_le_bytes());
            sent.extend(value);
            let response = checked(&sent, exchange(&sent)?, 0x13)?;
            ensure!(response.len() == 1, "Invalid ATT write response");
            Ok(json!({
                "status": "response",
                "handle": h
            }))
        }
        "discover" => {
            let kind = request["kind"].as_str().unwrap_or("services");
            ensure!(
                matches!(kind, "services" | "characteristics" | "descriptors"),
                "Unknown GATT discovery kind"
            );
            let mut start = request["start"].as_u64().unwrap_or(1);
            let end = request["end"].as_u64().unwrap_or(65535);
            ensure!(
                start > 0 && start <= end && end <= 65535,
                "Invalid GATT discovery range"
            );
            let mut rows = Vec::new();
            while start <= end {
                let mut sent = vec![match kind {
                    "services" => 0x10,
                    "characteristics" => 8,
                    _ => 4,
                }];
                sent.extend((start as u16).to_le_bytes());
                sent.extend((end as u16).to_le_bytes());
                if kind != "descriptors" {
                    sent.extend(if kind == "services" {
                        [0, 0x28]
                    } else {
                        [3, 0x28]
                    });
                }
                let raw = exchange(&sent)?;
                if raw.len() == 5 && raw[0] == 1 && raw[1] == sent[0] && raw[4] == 0x0a {
                    break;
                }
                let response = checked(&sent, raw, sent[0] + 1)?;
                ensure!(
                    response.len() >= 2 && response.len() <= 23,
                    "Invalid ATT discovery response"
                );
                let width = if kind == "descriptors" {
                    match response[1] {
                        1 => 4,
                        2 => 18,
                        _ => bail!("Invalid ATT information format"),
                    }
                } else {
                    response[1] as usize
                };
                ensure!(
                    match kind {
                        "services" => matches!(width, 6 | 20),
                        "characteristics" => matches!(width, 7 | 21),
                        _ => true,
                    },
                    "Invalid ATT entry width"
                );
                ensure!(
                    response.len() > 2 && (response.len() - 2) % width == 0,
                    "Truncated ATT discovery entry"
                );
                for entry in response[2..].chunks(width) {
                    let h = u16_at(entry, 0)? as u64;
                    ensure!(
                        h >= start && h <= end,
                        "Nonprogressing ATT discovery handle"
                    );
                    let row = match kind {
                        "services" => {
                            let last = u16_at(entry, 2)? as u64;
                            ensure!(last >= h && last <= end, "Invalid GATT service range");
                            start = last + 1;
                            json!({
                                "handle": h,
                                "end_handle": last,
                                "uuid": uuid(&entry[4..])?
                            })
                        }
                        "characteristics" => {
                            let value = u16_at(entry, 3)? as u64;
                            ensure!(
                                value > h && value <= end,
                                "Invalid characteristic value handle"
                            );
                            start = h + 1;
                            json!({
                                "handle": h,
                                "value_handle": value,
                                "properties": entry[2],
                                "uuid": uuid(&entry[5..])?
                            })
                        }
                        _ => {
                            start = h + 1;
                            json!({
                                "handle": h,
                                "uuid": uuid(&entry[2..])?
                            })
                        }
                    };
                    rows.push(row);
                    ensure!(rows.len() <= 256, "GATT discovery capacity exceeded");
                }
            }
            Ok(json!({
                "status": "response",
                "kind": kind,
                "attributes": rows
            }))
        }
        _ => bail!("Unsupported GATT operation"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn discovery_uses_peer_handles_and_preserves_errors() {
        let mut n = 0;
        let out = execute(&json!({"action": "discover"}), |p| {
            n += 1;
            if n == 1 {
                assert_eq!(p, [0x10, 1, 0, 255, 255, 0, 0x28]);
                Ok(vec![0x11, 6, 7, 0, 19, 0, 0x0f, 0x18])
            } else {
                assert_eq!(&p[1..3], [20, 0]);
                Ok(vec![1, 0x10, 20, 0, 0x0a])
            }
        })
        .unwrap();
        assert_eq!(out["attributes"][0]["handle"], 7);
        assert_eq!(out["attributes"][0]["uuid"], "180f");
        let e = execute(
            &json!({
                "action": "read",
                "handle": 9
            }),
            |_| {
                Ok(vec![1, 10, 9, 0, 5])
            }
        )
        .unwrap_err();
        assert!(e.to_string().contains("code=0x05"));
    }

    #[test]
    fn malformed_entries_and_oversized_writes_fail() {
        assert!(execute(&json!({"action": "discover"}), |_| Ok(vec![17, 6, 1])).is_err());
        assert!(
            execute(
                &json!({
                    "action": "write",
                    "handle": 1,
                    "value": "aa".repeat(513)
                }),
                |_| panic!()
            )
            .is_err()
        );
        assert!(
            execute(&json!({"action": "discover"}), |_| Ok(vec![
                17, 6, 0, 0, 0, 0, 0, 0
            ]))
            .is_err()
        );
    }
}

#[derive(Debug)]
struct AwaitResponse;

impl std::fmt::Display for AwaitResponse {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("Awaiting ATT response")
    }
}

impl std::error::Error for AwaitResponse {}

struct Operation {
    id: String,
    request: Value,
    deadline: Instant,
    responses: Vec<(Vec<u8>, Vec<u8>)>,
    waiting: Option<Vec<u8>>,
    token: u64,
    result: Value,
}

impl Operation {
    fn new(request: Value) -> Result<Self> {
        let timeout = request["timeout_ms"].as_u64().unwrap_or(10000);
        ensure!(
            (1..=120000).contains(&timeout),
            "BLE timeout must be 1..120000 ms"
        );
        Ok(Self {
            id: Uuid::new_v4().simple().to_string(),
            request,
            deadline: Instant::now() + Duration::from_millis(timeout),
            responses: Vec::new(),
            waiting: None,
            token: 0,
            result: json!({"status": "pending"}),
        })
    }

    fn advance(&mut self) -> Result<Option<Vec<u8>>> {
        let mut index = 0;
        let mut next = None;
        let result = execute(&self.request, |packet| {
            if let Some((sent, response)) = self.responses.get(index) {
                ensure!(packet == sent, "ATT transaction replay diverged");
                index += 1;
                return Ok(response.clone());
            }
            next = Some(packet.to_vec());
            Err(AwaitResponse.into())
        });
        match result {
            Ok(value) => {
                self.result = value;
                Ok(None)
            }
            Err(error) if error.is::<AwaitResponse>() => {
                self.waiting = next.clone();
                Ok(next)
            }
            Err(error) => {
                self.result = json!({
                    "status": "error",
                    "error": error.to_string()
                });
                if let Some(att) = error.downcast_ref::<AttFailure>() {
                    self.result["att_error"] = json!({
                        "opcode": att.opcode,
                        "handle": att.handle,
                        "code": att.code
                    });
                }
                Ok(None)
            }
        }
    }
}

/// One owned central per run. The QEMU transport supplies actual peer PDUs;
/// host polling only observes results and never schedules radio events.
#[derive(Default)]
pub struct Central {
    peer: Option<String>,
    connection: Option<String>,
    operation: Option<Operation>,
    cursor: u64,
    sequence: u64,
    token: u64,
    events: VecDeque<Value>,
    state: Value,
    ended: bool,
}

impl Central {
    pub fn active(&self) -> bool {
        self.peer.is_some()
    }

    fn snapshot(&self) -> Value {
        json!({
            "peer": self.peer,
            "connection": self.connection,
            "link": self.state,
            "sequence": self.sequence,
            "operation": self.operation.as_ref().map(|o| {
                let mut v = o.result.clone();
                v["id"] = json!(o.id);
                v
            })
        })
    }

    fn record(&mut self, mut event: Value) {
        self.sequence += 1;
        event["sequence"] = json!(self.sequence);
        event["peer"] = json!(self.peer);
        event["connection"] = json!(self.connection);
        self.events.push_back(event);
        if self.events.len() > 512 {
            self.events.pop_front();
        }
    }

    fn finish(&mut self, status: &str, error: &str) {
        if let Some(op) = &mut self.operation {
            if op.result["status"] == "pending" {
                op.result = json!({
                    "status": status,
                    "error": error
                });
            }
        }
    }

    pub fn ended(&mut self) {
        self.ended = true;
        self.finish("interrupted", "Run ended");
        self.state["connected"] = json!(false);
    }

    pub fn poll(&mut self, mut transport: impl FnMut(&Value) -> Result<Value>) -> Result<()> {
        if !self.active() {
            return Ok(());
        }
        for _ in 0..4 {
            let page = transport(&json!({
                "action": "events",
                "cursor": self.cursor
            }))?;
            let lost = page["lost"].as_u64().context("Invalid BLE history")?;
            if lost > 0 {
                self.record(json!({
                    "kind": "history_lost",
                    "lost": lost
                }));
                self.finish("error", "BLE transport history lost; reconnect required");
                transport(&json!({"action": "cancel"}))?;
            }
            self.cursor = page["cursor"].as_u64().context("Invalid BLE cursor")?;
            let events = page["events"]
                .as_array()
                .context("Invalid BLE event page")?;
            for event in events {
                let mut event = event.clone();
                event["transport_sequence"] = event["sequence"].clone();
                if event["kind"] == "advertisement" {
                    let raw = decode_hex(
                        event["value"].as_str().context("Missing advertising PDU")?,
                        39,
                    )?;
                    ensure!(raw.len() >= 8, "Truncated BLE advertising PDU");
                    event["pdu"] = event["value"].clone();
                    event["pdu_type"] = json!(raw[0] & 15);
                    event["address"] = json!(
                        raw[2..8]
                            .iter()
                            .rev()
                            .map(|b| format!("{b:02X}"))
                            .collect::<Vec<_>>()
                            .join(":")
                    );
                    event["address_type"] = json!(if raw[0] & 0x40 == 0 {
                        "public"
                    } else {
                        "random"
                    });
                    event["value"] = json!(hex(&raw[8..]));
                } else if event["kind"] == "att" {
                    let data =
                        decode_hex(event["value"].as_str().context("Missing ATT event")?, 517)?;
                    if matches!(data.first(), Some(0x1b | 0x1d)) {
                        ensure!(data.len() >= 3, "Truncated ATT notification");
                        event["kind"] = json!(if data[0] == 0x1b {
                            "notification"
                        } else {
                            "indication"
                        });
                        event["handle"] = json!(u16_at(&data, 1)?);
                        event["value"] = json!(hex(&data[3..]));
                    } else if data.first().is_some_and(|op| {
                        matches!(op, 1 | 5 | 7 | 9 | 0x0b | 0x0d | 0x11 | 0x13 | 0x17 | 0x19)
                    }) {
                        if let Some(op) = &mut self.operation {
                            if op.result["status"] == "pending" {
                                if let Some(sent) = op.waiting.take() {
                                    op.responses.push((sent, data));
                                    ensure!(
                                        op.responses.len() <= 257,
                                        "ATT transaction limit exceeded"
                                    );
                                }
                            }
                        }
                    }
                } else if event["kind"] == "delivered" {
                    if let Some(op) = &mut self.operation {
                        if op.result["status"] == "pending" && event["token"] == op.token {
                            if op
                                .waiting
                                .as_ref()
                                .is_some_and(|p| p.first() == Some(&0x52))
                            {
                                op.responses.push((op.waiting.take().unwrap(), Vec::new()));
                            }
                        }
                    }
                }
                let reset =
                    event["kind"] == "reset" && event["generation"] != self.state["generation"];
                self.record(event);
                if reset {
                    self.finish("interrupted", "BLE controller reset");
                    self.connection = None;
                }
            }
            if events.len() < 64 {
                break;
            }
        }
        self.state = transport(&json!({"action": "status"}))?;
        if self.state["error"].is_string() {
            let reason = self.state["error"].as_str().unwrap().to_owned();
            if self.state["connected"] == false
                && self.state["connecting"] == false
                && self
                    .operation
                    .as_ref()
                    .is_some_and(|o| o.request["action"] == "disconnect")
            {
                if let Some(op) = &mut self.operation {
                    op.result = json!({
                        "status": "disconnected",
                        "reason": reason
                    });
                }
            } else {
                self.finish("interrupted", &reason);
            }
        }
        let mut packet = None;
        let mut cancel = false;
        if let Some(op) = &mut self.operation {
            if op.result["status"] == "pending" {
                if Instant::now() >= op.deadline {
                    op.result = json!({
                        "status": "timeout",
                        "error": "BLE operation deadline expired"
                    });
                    cancel = true;
                } else {
                    match op.request["action"].as_str() {
                        Some("connect") if self.state["confirmed"] == true => {
                            op.result = json!({"status": "connected"});
                        }
                        Some("connect" | "disconnect") => {}
                        _ if op.waiting.is_none() => {
                            packet = op.advance()?;
                        }
                        _ => {}
                    }
                    if op.result["status"] == "error"
                        && !op.responses.is_empty()
                        && (op.result["att_error"].is_null()
                            || op.responses.iter().any(|(sent, _)| sent[0] == 0x16))
                    {
                        // Malformed replies cannot be correlated safely; a failed
                        // prepare must not leave queued values on a reusable link.
                        cancel = true;
                    }
                }
            }
        }
        if cancel {
            self.state = transport(&json!({"action": "cancel"}))?;
        }
        if let Some(packet) = packet {
            self.token += 1;
            let op = self.operation.as_mut().unwrap();
            op.token = self.token;
            self.state = transport(&json!({
                "action": "att",
                "value": hex(&packet),
                "token": self.token
            }))?;
        }
        Ok(())
    }

    pub fn call(
        &mut self,
        request: &Value,
        mut transport: impl FnMut(&Value) -> Result<Value>,
    ) -> Result<Value> {
        let object = request
            .as_object()
            .context("BLE request must be an object")?;
        ensure!(
            object.keys().all(|key| [
                "id",
                "worker",
                "run",
                "action",
                "peer",
                "connection",
                "operation",
                "address",
                "address_type",
                "kind",
                "handle",
                "start",
                "end",
                "value",
                "response",
                "after",
                "limit",
                "timeout_ms",
                "enabled"
            ]
            .contains(&key.as_str())),
            "Unknown BLE request field"
        );
        let action = request["action"].as_str().context("Missing BLE action")?;
        let fields: &[&str] = match action {
            "connect" => &["address", "address_type", "timeout_ms"],
            "discover" => &["kind", "start", "end", "timeout_ms"],
            "read" => &["handle", "timeout_ms"],
            "write" | "subscribe" => &["handle", "value", "response", "timeout_ms"],
            "disconnect" => &["timeout_ms"],
            "events" => &["after", "limit"],
            "operation" | "cancel" => &["operation"],
            "trace" => &["enabled"],
            "capabilities" | "open" | "status" | "scan" | "close" => &[],
            _ => bail!("Unknown BLE action"),
        };
        ensure!(
            object.keys().all(
                |key| ["id", "worker", "run", "action", "peer", "connection"]
                    .contains(&key.as_str())
                    || fields.contains(&key.as_str())
            ),
            "Unexpected field for BLE action"
        );
        for key in ["handle", "start", "end", "after", "limit", "timeout_ms"] {
            if let Some(value) = request.get(key) {
                ensure!(
                    value.as_u64().is_some(),
                    "BLE {key} must be an unsigned integer"
                );
            }
        }
        if let Some(response) = request.get("response") {
            ensure!(response.is_boolean(), "BLE response must be boolean");
        }

        if action == "capabilities" {
            return Ok(
                json!({
                    "role": "central",
                    "phy": ["1m"],
                    "advertising": "legacy",
                    "security": ["plaintext"],
                    "mtu": 23,
                    "max_write_command": 20,
                    "max_attribute": 512,
                    "connections": 1,
                    "event_capacity": 512,
                    "max_discovery_attributes": 256,
                    "pending_operations": 1,
                    "long_write": "prepare_execute",
                    "pairing": false,
                    "encryption": false
                }),
            );
        }
        if action == "open" {
            ensure!(!self.active(), "BLE central is already owned");
            *self = Self::default();
            self.state = transport(&json!({"action": "cancel"}))?;
            self.cursor = self.state["sequence"]
                .as_u64()
                .context("Missing BLE cursor")?;
            self.peer = Some(Uuid::new_v4().simple().to_string());
            return Ok(self.snapshot());
        }
        ensure!(
            self.peer.is_some() && request["peer"].as_str() == self.peer.as_deref(),
            "BLE peer ownership mismatch"
        );
        if let Some(connection) = request.get("connection") {
            ensure!(
                self.connection.is_some() && connection.as_str() == self.connection.as_deref(),
                "Stale BLE connection"
            );
        }
        ensure!(
            !self.ended || matches!(action, "status" | "events" | "operation" | "close"),
            "BLE run ended"
        );
        if !self.ended {
            self.poll(&mut transport)?;
        }
        // Polling may observe a controller reset and retire the connection.
        if let Some(connection) = request.get("connection") {
            ensure!(
                self.connection.is_some() && connection.as_str() == self.connection.as_deref(),
                "Stale BLE connection"
            );
        }
        match action {
            "status" => {}
            "trace" => {
                ensure!(
                    request["enabled"].is_boolean(),
                    "BLE trace enabled must be boolean"
                );
                self.state = transport(&json!({
                    "action": "trace",
                    "enabled": request["enabled"]
                }))?;
            }
            "events" => {
                let after = request["after"].as_u64().unwrap_or(0);
                let limit = request["limit"].as_u64().unwrap_or(128);
                ensure!(
                    after <= self.sequence && (1..=128).contains(&limit),
                    "Invalid BLE event page"
                );
                let first = self
                    .events
                    .front()
                    .and_then(|e| e["sequence"].as_u64())
                    .unwrap_or(self.sequence + 1);
                let rows: Vec<_> = self
                    .events
                    .iter()
                    .filter(|e| e["sequence"].as_u64().unwrap() > after)
                    .take(limit as usize)
                    .cloned()
                    .collect();
                let cursor = rows
                    .last()
                    .map(|e| e["sequence"].as_u64().unwrap())
                    .unwrap_or(after);
                return Ok(
                    json!({
                        "peer": self.peer,
                        "connection": self.connection,
                        "events": rows,
                        "cursor": cursor,
                        "lost": first.saturating_sub(after + 1),
                        "link": self.state
                    }),
                );
            }
            "operation" => {
                ensure!(
                    self.operation
                        .as_ref()
                        .is_some_and(|o| request["operation"] == o.id),
                    "Stale BLE operation"
                );
            }
            "scan" => {
                ensure!(
                    self.operation
                        .as_ref()
                        .is_none_or(|o| o.result["status"] != "pending"),
                    "BLE operation is pending"
                );
                self.state = transport(&json!({"action": "scan"}))?;
                self.connection = None;
            }
            "connect" | "discover" | "read" | "write" | "subscribe" | "disconnect" => {
                ensure!(
                    self.operation
                        .as_ref()
                        .is_none_or(|o| o.result["status"] != "pending"),
                    "BLE operation is pending"
                );
                if action != "connect" {
                    ensure!(
                        request["connection"].as_str() == self.connection.as_deref()
                            && self.connection.is_some(),
                        "Missing current BLE connection"
                    );
                    if action != "disconnect" {
                        ensure!(
                            self.state["connected"] == true && self.state["confirmed"] == true,
                            "BLE link is not connected"
                        );
                    }
                }
                let mut op = Operation::new(request.clone())?;
                if action == "connect" {
                    let address = request["address"].as_str().context("Missing BLE address")?;
                    let parts: Vec<_> = address.split(':').collect();
                    ensure!(
                        parts.len() == 6 && parts.iter().all(|p| p.len() == 2),
                        "Expected XX:XX:XX:XX:XX:XX BLE address"
                    );
                    let address = decode_hex(&parts.into_iter().rev().collect::<String>(), 6)?;
                    let address_type = match request["address_type"].as_str() {
                        Some("public") => 0,
                        Some("random") => 1,
                        _ => bail!("BLE address type must be public or random"),
                    };
                    let state = transport(
                        &json!({
                            "action": "connect",
                            "address": hex(&address),
                            "address_type": address_type
                        }),
                    )?;
                    self.state = state;
                    self.connection = Some(Uuid::new_v4().simple().to_string());
                } else if action == "disconnect" {
                    // The transport checks current link state atomically with
                    // queuing termination; a remote end is already quiescent.
                    self.state = transport(&json!({"action": "disconnect"}))?;
                    if self.state["connected"] == false && self.state["connecting"] == false {
                        op.result = json!({
                            "status": "disconnected",
                            "reason": self.state["error"]
                        });
                    }
                } else if let Some(packet) = op.advance()? {
                    self.token += 1;
                    op.token = self.token;
                    self.state = transport(
                        &json!({
                            "action": "att",
                            "value": hex(&packet),
                            "token": self.token
                        }),
                    )?;
                }
                self.operation = Some(op);
            }
            "cancel" => {
                ensure!(
                    self.operation
                        .as_ref()
                        .is_some_and(|o| request["operation"] == o.id),
                    "Stale BLE operation"
                );
                if self
                    .operation
                    .as_ref()
                    .is_some_and(|o| o.result["status"] == "pending")
                {
                    self.finish("cancelled", "Cancelled by owner; remote observes link loss");
                    self.state = transport(&json!({"action": "cancel"}))?;
                }
            }
            "close" => {
                self.finish("cancelled", "Central closed by owner");
                if !self.ended {
                    self.state = transport(&json!({"action": "cancel"}))?;
                }
                let result = self.snapshot();
                self.peer = None;
                return Ok(result);
            }
            _ => bail!("Unknown BLE action"),
        }
        Ok(self.snapshot())
    }
}

#[cfg(test)]
mod lifecycle_tests {
    use super::*;
    #[test]
    fn long_attribute_prepare_execute_and_blob_boundaries() {
        let expected: Vec<u8> = (0..512).map(|n| n as u8).collect();
        let mut staged = Vec::new();
        let result = execute(
            &json!({
                "action": "write",
                "handle": 73,
                "value": hex(&expected)
            }),
            |p| {
                if p[0] == 0x18 {
                    assert_eq!(p, [0x18, 1]);
                    assert_eq!(staged, expected);
                    return Ok(vec![0x19]);
                }
                assert_eq!(p[0], 0x16);
                assert_eq!(u16_at(p, 1)?, 73);
                assert_eq!(u16_at(p, 3)? as usize, staged.len());
                assert!(p.len() <= 23);
                staged.extend_from_slice(&p[5..]);
                let mut response = p.to_vec();
                response[0] = 0x17;
                Ok(response)
            },
        )
        .unwrap();
        assert_eq!(result["status"], "response");
        let read = execute(
            &json!({
                "action": "read",
                "handle": 73
            }),
            |p| {
                let offset = if p[0] == 10 {
                    0
                } else {
                    u16_at(p, 3)? as usize
                };
                let mut response = vec![p[0] + 1];
                response.extend_from_slice(&expected[offset..(offset + 22).min(512)]);
                Ok(response)
            }
        )
        .unwrap();
        assert_eq!(read["value"], hex(&expected));
        let e = execute(
            &json!({
                "action": "write",
                "handle": 73,
                "value": "aa".repeat(21)
            }),
            |p| Ok(vec![1, p[0], 73, 0, 3]),
        )
        .unwrap_err();
        assert!(e.to_string().contains("code=0x03"));
    }

    #[test]
    fn asynchronous_transactions_ownership_stale_ids_and_cancellation() {
        let mut central = Central::default();
        let mut events = Vec::new();
        let mut sequence = 0u64;
        let mut connected = false;
        let mut transport = |p: &Value| -> Result<Value> {
            match p["action"].as_str().unwrap() {
                "connect" => connected = true,
                "cancel" => connected = false,
                "events" => {
                    return Ok(
                        json!({
                            "cursor": sequence,
                            "lost": 0,
                            "events": std::mem::take(&mut events)
                        }),
                    );
                }
                "att" => {
                    let sent = decode_hex(p["value"].as_str().unwrap(), 23)?;
                    if sent[0] == 10 {
                        sequence += 1;
                        events.push(json!({
                            "sequence": sequence,
                            "kind": "att",
                            "value": "0b001122",
                            "virtual_ns": 123
                        }));
                    }
                }
                _ => {}
            }
            Ok(
                json!({
                    "sequence": sequence,
                    "connected": connected,
                    "confirmed": connected,
                    "error": null
                }),
            )
        };
        let peer = central
            .call(&json!({"action": "open"}), &mut transport)
            .unwrap()["peer"]
            .clone();
        assert!(
            central
                .call(
                    &json!({
                        "action": "scan",
                        "peer": "another"
                    }),
                    &mut transport
                )
                .is_err()
        );
        let connection = central
            .call(
                &json!({
                    "action": "connect",
                    "peer": peer,
                    "address": "06:05:04:03:02:01",
                    "address_type": "public"
                }),
                &mut transport
            )
            .unwrap()["connection"]
            .clone();
        let request = json!({
            "peer": peer,
            "connection": connection
        });
        let mut read = request.clone();
        read["action"] = json!("read");
        read["handle"] = json!(73);
        central.poll(&mut transport).unwrap();
        let response = central.call(&read, &mut transport).unwrap();
        assert_eq!(response["operation"]["status"], "pending");
        central.poll(&mut transport).unwrap();
        assert_eq!(central.snapshot()["operation"]["value"], "001122");
        let completed = central.snapshot()["operation"]["id"].clone();
        let mut done_cancel = request.clone();
        done_cancel["action"] = json!("cancel");
        done_cancel["operation"] = completed;
        let done = central.call(&done_cancel, &mut transport).unwrap();
        assert_eq!(done["operation"]["status"], "response");
        assert_eq!(done["link"]["confirmed"], true);
        let mut write = request.clone();
        write["action"] = json!("write");
        write["handle"] = json!(74);
        write["value"] = json!("1122");
        let response = central.call(&write, &mut transport).unwrap();
        let operation = response["operation"]["id"].clone();
        assert!(central.call(&write, &mut transport).is_err());
        let mut cancel = request;
        cancel["action"] = json!("cancel");
        cancel["operation"] = operation;
        assert_eq!(
            central.call(&cancel, &mut transport).unwrap()["operation"]["status"],
            "cancelled"
        );
        cancel["operation"] = json!("old");
        assert!(central.call(&cancel, &mut transport).is_err());
        read["connection"] = json!("old");
        assert!(central.call(&read, &mut transport).is_err());
    }

    #[test]
    fn disconnect_confirms_same_connection_across_remote_end_races() {
        for phase in [
            "before_status",
            "before_submit",
            "in_transport",
            "after_submit",
        ] {
            let mut central = Central {
                peer: Some("peer".into()),
                connection: Some("connection".into()),
                ..Central::default()
            };
            let mut active = phase != "before_status";
            let mut stopped = false;
            let mut terminations = 0;
            let request = json!({
                "peer": "peer",
                "connection": "connection"
            });
            let mut transport = |p: &Value| -> Result<Value> {
                if p["action"] == "events" {
                    return Ok(json!({
                        "lost": 0,
                        "cursor": 0,
                        "events": []
                    }));
                }
                if p["action"] == "disconnect" {
                    if phase == "in_transport" {
                        active = false;
                    }
                    terminations += usize::from(active);
                    stopped = true;
                }
                if stopped && phase == "after_submit" && p["action"] == "status" {
                    active = false;
                }
                Ok(json!({
                    "generation": 1,
                    "connected": active,
                    "confirmed": active,
                    "connecting": false,
                    "error": if active {
                        Value::Null
                    } else {
                        json!("remote_disconnect")
                    }
                }))
            };
            let mut status = request.clone();
            status["action"] = json!("status");
            let observed = central.call(&status, &mut transport).unwrap();
            assert_eq!(observed["link"]["connected"], phase != "before_status");
            // Independent calls model the remote event between host RPCs.
            if phase == "before_submit" {
                drop(transport);
                let mut down = |p: &Value| -> Result<Value> {
                    if p["action"] == "events" {
                        return Ok(json!({
                            "lost": 0,
                            "cursor": 0,
                            "events": []
                        }));
                    }
                    Ok(json!({
                        "generation": 1,
                        "connected": false,
                        "confirmed": false,
                        "connecting": false,
                        "error": "remote_disconnect"
                    }))
                };
                let mut disconnect = request.clone();
                disconnect["action"] = json!("disconnect");
                let result = central.call(&disconnect, &mut down).unwrap();
                assert_eq!(result["operation"]["status"], "disconnected");
                assert_eq!(result["operation"]["reason"], "remote_disconnect");
                continue;
            }
            let mut disconnect = request.clone();
            disconnect["action"] = json!("disconnect");
            let result = central.call(&disconnect, &mut transport).unwrap();
            if phase == "after_submit" {
                assert_eq!(result["operation"]["status"], "pending");
                central.poll(&mut transport).unwrap();
            }
            assert_eq!(central.snapshot()["operation"]["status"], "disconnected");
            assert_eq!(
                central.snapshot()["operation"]["reason"],
                "remote_disconnect"
            );
            drop(transport);
            assert_eq!(terminations, usize::from(phase == "after_submit"));
        }
    }

    #[test]
    fn disconnect_rejects_foreign_identity_reset_and_transport_errors() {
        for failure in ["peer", "connection", "reset", "transport"] {
            let mut central = Central {
                peer: Some("peer".into()),
                connection: Some("connection".into()),
                state: json!({"generation": 1}),
                ..Central::default()
            };
            let mut request = json!({
                "action": "disconnect",
                "peer": "peer",
                "connection": "connection"
            });
            if matches!(failure, "peer" | "connection") {
                request[failure] = json!("another");
            }
            let mut mutations = 0;
            let mut transport = |p: &Value| -> Result<Value> {
                match p["action"].as_str().unwrap() {
                    "events" => Ok(json!({
                        "lost": 0,
                        "cursor": 0,
                        "events": if failure == "reset" {
                            json!([{
                                "kind": "reset",
                                "generation": 2
                            }])
                        } else {
                            json!([])
                        }
                    })),
                    "status" => Ok(json!({
                        "connected": false,
                        "connecting": false,
                        "confirmed": false
                    })),
                    "disconnect" => {
                        mutations += 1;
                        bail!("independent transport rejection")
                    }
                    _ => panic!("Unexpected transport mutation"),
                }
            };
            let error = central.call(&request, &mut transport).unwrap_err();
            assert!(error.to_string().contains(match failure {
                "peer" => "ownership",
                "connection" | "reset" => "Stale BLE connection",
                _ => "independent transport rejection",
            }));
            assert_eq!(mutations, usize::from(failure == "transport"));
        }
    }
}
