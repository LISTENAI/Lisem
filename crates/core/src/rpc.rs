//! Versioned native SDK contract. Worker endpoints retain their local token.
use anyhow::Error;
use serde_json::{Value, json};
use std::fmt;

pub const VERSION: u32 = 1;

#[derive(Debug)]
pub struct StaleRun;
impl fmt::Display for StaleRun {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("The request belongs to an earlier run")
    }
}
impl std::error::Error for StaleRun {}

pub fn error_kind(error: &Error) -> &'static str {
    if error.is::<StaleRun>() { "stale_run" }
    else if error.is::<crate::transport::ControlPending>() { "delivery_unknown" }
    else { "rejected" }
}

pub fn hello() -> Value {
    json!({"protocol":"lisem-native", "version":VERSION,
        "build":crate::identity::build(),
        "features":["explicit_uid", "worker_endpoint", "temporary_instances",
            "run_identity", "uart_history", "signal_trace", "virtual_events",
            "display_observation", "wifi_observation", "ble_central"]})
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn pending_delivery_and_stale_runs_keep_their_identity_through_context() {
        let stale = Error::new(StaleRun).context("control failed");
        assert_eq!(error_kind(&stale), "stale_run");
        let pending = Error::new(crate::transport::ControlPending).context("input not confirmed");
        assert_eq!(error_kind(&pending), "delivery_unknown");
        assert_eq!(error_kind(&anyhow::anyhow!("invalid input")), "rejected");
    }
}
