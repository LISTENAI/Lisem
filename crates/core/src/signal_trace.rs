//! Bounded digital transition history and cursor validation.
use anyhow::{Context, Result, ensure};
use schemars::JsonSchema;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::collections::BTreeSet;

#[derive(Clone, Copy, Default, Debug, Deserialize, Serialize, JsonSchema, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum Action {
    #[default]
    List,
    Start,
    Read,
    Stop,
}

#[derive(Deserialize, Serialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct Request {
    pub id: String,
    /// Current run from lisem_status; required for start/read/stop.
    pub run: Option<String>,
    #[serde(default)]
    pub action: Action,
    /// Capture ID returned by start; required for read and stop.
    pub capture: Option<String>,
    /// Exact source IDs or net names returned by list. Only valid for start.
    pub sources: Option<Vec<String>>,
    /// Event history capacity, 1..4096 and at least the selected source count.
    pub capacity: Option<usize>,
    /// Last consumed sequence, initially zero. A lost cursor is reported explicitly.
    pub after: Option<u64>,
    /// Maximum returned events, 1..512; defaults to 128.
    pub limit: Option<usize>,
}

impl Request {
    pub fn validate(&self) -> Result<()> {
        if self.action != Action::List {
            ensure!(
                self.run.is_some(),
                "Signal capture controls require a run identifier"
            );
        }
        match self.action {
            Action::Start => ensure!(
                self.sources.is_some()
                    && self.capture.is_none()
                    && self.after.is_none()
                    && self.limit.is_none(),
                "Start requires sources and no read/stop options"
            ),
            Action::Read | Action::Stop => {
                ensure!(
                    self.capture
                        .as_ref()
                        .is_some_and(|s| !s.is_empty() && s.len() <= 64),
                    "A capture identity is required"
                );
                ensure!(
                    self.sources.is_none() && self.capacity.is_none(),
                    "Sources and capacity only apply to start"
                );
                if self.action == Action::Stop {
                    ensure!(
                        self.after.is_none() && self.limit.is_none(),
                        "Read options only apply to read"
                    );
                }
            }
            Action::List => ensure!(
                self.sources.is_none()
                    && self.capacity.is_none()
                    && self.capture.is_none()
                    && self.after.is_none()
                    && self.limit.is_none(),
                "List does not accept capture options"
            ),
        }
        Ok(())
    }
}

pub fn endpoints(available: &Value, selectors: &[String], capacity: usize) -> Result<Vec<u64>> {
    ensure!(
        !selectors.is_empty() && selectors.len() <= 96,
        "Select 1..96 digital sources"
    );
    let rows = available
        .as_array()
        .context("Invalid digital source catalog")?;
    let mut selected = BTreeSet::new();
    for selector in selectors {
        let matches: Vec<_> = rows
            .iter()
            .filter(|row| {
                row["id"] == *selector
                    || row["nets"]
                        .as_array()
                        .is_some_and(|v| v.iter().any(|net| net == selector))
            })
            .collect();
        ensure!(
            !matches.is_empty(),
            "Unknown digital source or net: {selector}"
        );
        for source in matches {
            selected.insert(source["endpoint"].as_u64().context("Invalid endpoint")?);
        }
    }
    ensure!(
        (1..=4096).contains(&capacity) && selected.len() <= capacity,
        "Capacity must be 1..4096 and hold every selected source's initial state"
    );
    Ok(selected.into_iter().collect())
}

pub fn page(
    raw: &Value,
    capture: &str,
    run: &Value,
    available: &Value,
    after: u64,
    limit: usize,
) -> Result<Value> {
    ensure!(
        raw["id"] == capture,
        "The request belongs to an earlier signal capture"
    );
    ensure!((1..=512).contains(&limit), "Event limit must be 1..512");
    let next = raw["next"]
        .as_u64()
        .context("Invalid signal trace sequence")?;
    let oldest = raw["oldest"]
        .as_u64()
        .context("Invalid signal trace cursor")?;
    ensure!(
        next > 0 && oldest > 0 && oldest <= next && after < next,
        "Signal cursor is ahead of this capture"
    );
    let sources: Vec<_> = available
        .as_array()
        .context("Invalid digital source catalog")?
        .iter()
        .filter(|row| {
            raw["endpoints"]
                .as_array()
                .is_some_and(|pins| pins.contains(&row["endpoint"]))
        })
        .cloned()
        .collect();
    let rows = raw["events"].as_array().context("Invalid signal events")?;
    let mut events = Vec::new();
    for row in rows
        .iter()
        .filter(|r| r["sequence"].as_u64().is_some_and(|v| v > after))
        .take(limit)
    {
        let source = sources
            .iter()
            .find(|source| source["endpoint"] == row["endpoint"])
            .context("Signal event has an unknown endpoint")?;
        let mut event = row.clone();
        event["source"] = source["id"].clone();
        event["nets"] = source["nets"].clone();
        events.push(event);
    }
    let cursor = events
        .last()
        .and_then(|v| v["sequence"].as_u64())
        .unwrap_or(after);
    Ok(
        json!({"capture":capture,"run":run,"status":raw["status"],"reason":raw["reason"],
        "started_ns":raw["started_ns"],"ended_ns":raw["ended_ns"],"capacity":raw["capacity"],
        "sources":sources,"oldest":oldest,"next":next,"next_cursor":cursor,
        "evicted_events":raw["dropped"],"lost_events":oldest.saturating_sub(after + 1),
        "has_more":cursor + 1 < next,"events":events}),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sources() -> Value {
        json!([{"id":"chip.PB1","endpoint":33,"nets":["LED"]},
               {"id":"extio.PC3","endpoint":83,"nets":["PWM"]}])
    }

    #[test]
    fn selection_resolves_nets_and_deduplicates_pads() {
        assert_eq!(
            endpoints(&sources(), &["LED".into(), "chip.PB1".into()], 1).unwrap(),
            vec![33]
        );
        for (selectors, capacity) in [
            (vec!["missing".into()], 5),
            (vec![], 5),
            (vec!["LED".into(), "PWM".into()], 1),
            (vec!["LED".into()], 4097),
        ] {
            assert!(endpoints(&sources(), &selectors, capacity).is_err());
        }
    }

    #[test]
    fn cursors_distinguish_eviction_from_reader_loss() {
        let raw = json!({"id":"capture","status":"stopped","reason":"reset","capacity":3,
            "oldest":5,"next":8,"dropped":4,"endpoints":[33],"events":[
                {"sequence":5,"endpoint":33,"virtual_ns":100,"driven":true,"level":false},
                {"sequence":6,"endpoint":33,"virtual_ns":200,"driven":true,"level":true},
                {"sequence":7,"endpoint":33,"virtual_ns":300,"driven":true,"level":false}]});
        let run = json!("run");
        let first = page(&raw, "capture", &run, &sources(), 0, 2).unwrap();
        assert_eq!(first["lost_events"], 4);
        assert_eq!(first["evicted_events"], 4);
        assert_eq!(first["next_cursor"], 6);
        assert_eq!(first["has_more"], true);
        assert_eq!(first["events"][0]["source"], "chip.PB1");
        let second = page(&raw, "capture", &run, &sources(), 6, 2).unwrap();
        assert_eq!(second["lost_events"], 0);
        assert_eq!(second["next_cursor"], 7);
        assert_eq!(second["has_more"], false);
        let empty = page(&raw, "capture", &run, &sources(), 7, 2).unwrap();
        assert!(empty["events"].as_array().unwrap().is_empty());
        for (capture, cursor, limit) in [
            ("stale", 0, 1),
            ("capture", 8, 1),
            ("capture", u64::MAX, 1),
            ("capture", 0, 513),
        ] {
            assert!(page(&raw, capture, &run, &sources(), cursor, limit).is_err());
        }
    }

    #[test]
    fn requests_reject_unscoped_controls_and_conflicting_options() {
        for value in [
            json!({"id":"x","action":"start","sources":["LED"]}),
            json!({"id":"x","run":"r","action":"stop"}),
            json!({"id":"x","run":"r","action":"start","sources":[],"capture":"c"}),
            json!({"id":"x","action":"list","capacity":4}),
            json!({"id":"x","run":"r","action":"stop","capture":"c","after":0}),
        ] {
            assert!(
                serde_json::from_value::<Request>(value)
                    .unwrap()
                    .validate()
                    .is_err()
            );
        }
    }
}
