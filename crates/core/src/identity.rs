//! Compiled provenance stays valid when the installed executable is replaced.
use serde_json::{Value, json};

pub fn build() -> Value {
    json!({"version":env!("CARGO_PKG_VERSION"),
        "source_sha256":env!("LISEM_SOURCE_SHA256"),
        "target":env!("LISEM_BUILD_TARGET")})
}

/// Missing identities belong to older workers, not to matching builds.
pub fn comparison(client: &Value, worker: &Value) -> &'static str {
    match (
        client["source_sha256"].as_str(),
        worker["source_sha256"].as_str(),
    ) {
        (Some(a), Some(b)) if a == b && client["target"] == worker["target"] => "same",
        (Some(_), Some(_)) => "different",
        _ => "unknown",
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn absent_or_different_workers_are_not_reported_as_matching() {
        let current = build();
        assert_eq!(current["source_sha256"].as_str().unwrap().len(), 64);
        assert_eq!(comparison(&current, &current), "same");
        assert_eq!(comparison(&current, &Value::Null), "unknown");
        let mut other = current.clone();
        other["source_sha256"] = json!("older build");
        assert_eq!(comparison(&current, &other), "different");
    }
}
