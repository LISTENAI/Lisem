//! Run-bound rotary input. Completion confirms contact edges, not firmware behavior.
use anyhow::{Result, ensure};
use schemars::JsonSchema;
use serde::{Deserialize, Serialize};

#[derive(Deserialize, Serialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct Request {
    pub id: String,
    pub run: String,
    pub rotary: String,
    /// rotate, status or cancel.
    pub action: String,
    /// Signed detents, -32..32 excluding zero; positive means A leads B.
    pub detents: Option<i32>,
    /// Virtual milliseconds between edges, 1..100. Required for rotate.
    pub edge_ms: Option<u32>,
    /// Required for status/cancel; returned by rotate.
    pub sequence: Option<u64>,
}

impl Request {
    pub fn validate(&self) -> Result<()> {
        ensure!(!self.run.is_empty(), "Rotary input requires a run identifier");
        if self.action == "rotate" {
            ensure!(self.detents.is_some_and(|n| (-32..=32).contains(&n) && n != 0), "Invalid rotary detents");
            ensure!(self.edge_ms.is_some_and(|n| (1..=100).contains(&n)), "Invalid rotary edge interval");
            ensure!(self.sequence.is_none(), "Rotation creates a new sequence");
        } else {
            ensure!(["status", "cancel"].contains(&self.action.as_str()), "Unknown rotary action");
            ensure!(self.sequence.is_some_and(|n| n > 0 && n <= i64::MAX as u64), "A positive rotary sequence is required");
            ensure!(self.detents.is_none() && self.edge_ms.is_none(), "Only rotate accepts motion parameters");
        }
        Ok(())
    }
}
