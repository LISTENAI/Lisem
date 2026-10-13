//! Typed board inputs shared by runtime controls and front ends.
use anyhow::{Context, Result, ensure};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::collections::{BTreeMap, BTreeSet};

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
pub enum Input {
    Digital {
        id: String,
        label: String,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        description: Option<String>,
        pin: u8,
        default: bool,
    },
    Voltage {
        id: String,
        label: String,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        description: Option<String>,
        channel: u8,
        default: u32,
        max: u32,
        divider_numerator: u32,
        divider_denominator: u32,
    },
}

impl Input {
    pub fn id(&self) -> &str {
        match self {
            Self::Digital { id, .. } | Self::Voltage { id, .. } => id,
        }
    }
    fn default(&self) -> Value {
        match self {
            Self::Digital { default, .. } => json!(default),
            Self::Voltage { default, .. } => json!(default),
        }
    }
    fn validate(&self, value: &Value) -> Result<()> {
        match self {
            Self::Digital { .. } => ensure!(value.is_boolean(), "Digital input must be boolean"),
            Self::Voltage { max, .. } => ensure!(
                value.as_u64().is_some_and(|v| v <= *max as u64),
                "Voltage input must be an integer in 0..={max} mV"
            ),
        }
        Ok(())
    }
}

pub fn definitions(board: &Value) -> Result<Vec<Input>> {
    let inputs: Vec<Input> = if board["inputs"].is_null() {
        Vec::new()
    } else {
        serde_json::from_value(board["inputs"].clone())?
    };
    ensure!(inputs.len() <= 64, "Too many board inputs");
    let mut ids = BTreeSet::new();
    let mut targets = BTreeSet::new();
    for input in &inputs {
        ensure!(
            !input.id().is_empty()
                && input.id().len() <= 64
                && input
                    .id()
                    .bytes()
                    .all(|c| c.is_ascii_alphanumeric() || c == b'_')
                && ids.insert(input.id()),
            "Invalid or duplicate input ID"
        );
        input.validate(&input.default())?;
        match input {
            Input::Digital { pin, .. } => {
                ensure!(
                    *pin < 64 && targets.insert((0, *pin)),
                    "Invalid or duplicate input pad"
                );
                ensure!(
                    !board["buttons"]
                        .as_array()
                        .into_iter()
                        .flatten()
                        .any(|b| b["pin"].as_u64().map(|p| p + if b["bank"] == "B" {
                            32
                        } else {
                            0
                        }) == Some(*pin as u64)),
                    "Input pad is owned by a board button"
                );
            }
            Input::Voltage {
                channel,
                max,
                divider_numerator,
                divider_denominator,
                ..
            } => {
                ensure!(
                    (4..10).contains(channel) && targets.insert((1, *channel)),
                    "Invalid or duplicate VIN channel"
                );
                ensure!(
                    *max > 0
                        && *max <= 100_000
                        && *divider_numerator > 0
                        && *divider_numerator <= *divider_denominator
                        && *divider_denominator <= 1_000_000,
                    "Invalid voltage range or divider"
                );
                ensure!(
                    *max as u64 * 1000 * *divider_numerator as u64 / *divider_denominator as u64
                        <= 3_600_000,
                    "Voltage exceeds the ADC pad range"
                );
            }
        }
    }
    Ok(inputs)
}

pub fn overrides(board: &Value, value: &Value) -> Result<BTreeMap<String, Value>> {
    let values: BTreeMap<String, Value> = if value.is_null() {
        BTreeMap::new()
    } else {
        serde_json::from_value(value.clone())?
    };
    let inputs = definitions(board)?;
    for (id, value) in &values {
        inputs
            .iter()
            .find(|input| input.id() == id)
            .context("Unknown board input")?
            .validate(value)?;
    }
    Ok(values)
}

pub fn change(board: &Value, saved: &Value, changes: &Value) -> Result<Value> {
    let mut values = overrides(board, saved)?;
    let inputs = definitions(board)?;
    let changes = changes
        .as_object()
        .context("Input changes must be an object")?;
    ensure!(!changes.is_empty(), "Input changes must not be empty");
    for (id, value) in changes {
        let input = inputs
            .iter()
            .find(|input| input.id() == id)
            .context("Unknown board input")?;
        if value.is_null() {
            values.remove(id);
        } else {
            input.validate(value)?;
            values.insert(id.clone(), value.clone());
        }
    }
    Ok(serde_json::to_value(values)?)
}

pub fn state(board: &Value, saved: &Value) -> Result<Value> {
    let values = overrides(board, saved)?;
    let rows = definitions(board)?
        .iter()
        .map(|input| {
            let mut row = serde_json::to_value(input).unwrap();
            row["value"] = values
                .get(input.id())
                .cloned()
                .unwrap_or_else(|| input.default());
            row["overridden"] = json!(values.contains_key(input.id()));
            if matches!(input, Input::Voltage { .. }) {
                row["unit"] = json!("mV");
            }
            row
        })
        .collect::<Vec<_>>();
    Ok(json!(rows))
}

pub fn physical(board: &Value, saved: &Value) -> Result<Value> {
    let values = overrides(board, saved)?;
    let mut pads = BTreeMap::new();
    let mut adc_uv = BTreeMap::new();
    for input in definitions(board)? {
        let value = values
            .get(input.id())
            .cloned()
            .unwrap_or_else(|| input.default());
        match input {
            Input::Digital { pin, .. } => {
                pads.insert(pin.to_string(), value);
            }
            Input::Voltage {
                channel,
                divider_numerator,
                divider_denominator,
                ..
            } => {
                let uv = value.as_u64().unwrap() * 1000 * divider_numerator as u64
                    / divider_denominator as u64;
                adc_uv.insert(channel.to_string(), uv);
            }
        }
    }
    Ok(json!({"pads":pads,"adc_uv":adc_uv}))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn typed_values_release_and_board_divider() {
        let board: Value =
            serde_json::from_str(include_str!("../../../boards/arcs-mini.json")).unwrap();
        let value = change(
            &board,
            &Value::Null,
            &json!({"usb_det":false,"battery_voltage":3800}),
        )
        .unwrap();
        assert_eq!(physical(&board, &value).unwrap()["adc_uv"]["7"], 1_520_000);
        assert_eq!(physical(&board, &value).unwrap()["pads"]["32"], false);
        assert!(change(&board, &value, &json!({"battery_voltage":4501})).is_err());
        assert!(change(&board, &value, &json!({"usb_det":1})).is_err());
        assert!(change(&board, &value, &json!({"unknown":null})).is_err());
        let restored = change(&board, &value, &json!({"usb_det":null})).unwrap();
        assert_eq!(physical(&board, &restored).unwrap()["pads"]["32"], true);
        assert_eq!(restored["battery_voltage"], 3800);
    }
}
