//! External logical Wi-Fi environment, independent of board wiring.
use anyhow::{Result, ensure};
use serde::{Deserialize, Serialize};
use serde_json::Value;

pub const MAX_ACCESS_POINTS: usize = 16;

#[derive(Clone, Copy, Debug, Default, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "kebab-case")]
pub enum Security {
    #[default]
    Open,
    Wpa2Psk,
}
impl Security {
    fn is_open(&self) -> bool {
        *self == Self::Open
    }
}

#[derive(Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct AccessPoint {
    pub ssid: String,
    pub rssi: i16,
    pub channel: u8,
    #[serde(alias = "connectable")]
    pub internet: bool,
    #[serde(default, skip_serializing_if = "Security::is_open")]
    pub security: Security,
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub password: String,
}

impl std::fmt::Debug for AccessPoint {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("AccessPoint")
            .field("ssid", &self.ssid)
            .field("rssi", &self.rssi)
            .field("channel", &self.channel)
            .field("internet", &self.internet)
            .field("security", &self.security)
            .field("password", &"[redacted]")
            .finish()
    }
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct WifiEnvironment {
    pub access_points: Vec<AccessPoint>,
}

impl Default for WifiEnvironment {
    fn default() -> Self {
        Self {
            access_points: vec![AccessPoint {
                ssid: "Lisem".into(),
                rssi: -45,
                channel: 1,
                internet: true,
                security: Security::Open,
                password: String::new(),
            }],
        }
    }
}

impl WifiEnvironment {
    pub fn from_value(value: &Value) -> Result<Self> {
        let environment: Self = if value.is_null() {
            Self::default()
        } else {
            serde_json::from_value(value.clone())?
        };
        environment.validate()?;
        Ok(environment)
    }

    pub fn validate(&self) -> Result<()> {
        ensure!(
            self.access_points.len() <= MAX_ACCESS_POINTS,
            "Wi-Fi environment supports at most 16 access points"
        );
        for ap in &self.access_points {
            ensure!(
                !ap.ssid.is_empty() && ap.ssid.len() <= 32 && !ap.ssid.contains('\0'),
                "SSID must contain 1..32 UTF-8 bytes and no NUL"
            );
            match ap.security {
                Security::Open => ensure!(
                    ap.password.is_empty(),
                    "Open access points cannot have a password"
                ),
                Security::Wpa2Psk => ensure!(
                    (8..=63).contains(&ap.password.len())
                        && ap.password.bytes().all(|b| (32..=126).contains(&b)),
                    "WPA2-PSK password must contain 8..63 printable ASCII characters"
                ),
            }
            ensure!((-127..=0).contains(&ap.rssi), "RSSI must be -127..0 dBm");
            ensure!(
                (1..=14).contains(&ap.channel),
                "Wi-Fi channel must be 1..14"
            );
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn security_requires_real_password_and_redacts_debug() {
        let mut env = WifiEnvironment::default();
        env.access_points[0].security = Security::Wpa2Psk;
        assert!(env.validate().is_err());
        env.access_points[0].password = "test-password".into();
        assert!(env.validate().is_ok());
        assert!(!format!("{env:?}").contains("test-password"));
        assert_eq!(
            WifiEnvironment::from_value(&serde_json::to_value(&env).unwrap()).unwrap(),
            env
        );
        for value in [
            "short".to_owned(),
            "x".repeat(64),
            "validpass\0".to_owned(),
            "密码12345678".to_owned(),
        ] {
            env.access_points[0].password = value;
            assert!(env.validate().is_err());
        }
        env.access_points[0].security = Security::Open;
        assert!(env.validate().is_err());
    }

    #[test]
    fn defaults_and_empty_environment_are_distinct() {
        assert_eq!(
            WifiEnvironment::from_value(&Value::Null)
                .unwrap()
                .access_points[0]
                .ssid,
            "Lisem"
        );
        let empty = WifiEnvironment::from_value(&json!({"access_points":[]})).unwrap();
        assert!(empty.access_points.is_empty());
        assert_eq!(
            WifiEnvironment::from_value(&serde_json::to_value(&empty).unwrap()).unwrap(),
            empty
        );
    }

    #[test]
    fn validate_boundaries_and_preserve_duplicate_ssids() {
        let mut env = WifiEnvironment::default();
        env.access_points[0].ssid = "热点".repeat(5);
        env.access_points[0].rssi = -127;
        env.access_points[0].channel = 14;
        env.access_points[0].internet = false;
        env.access_points = vec![env.access_points[0].clone(); MAX_ACCESS_POINTS];
        assert!(env.validate().is_ok());
        env.access_points.push(env.access_points[0].clone());
        assert!(env.validate().is_err());
        env.access_points.truncate(1);
        env.access_points[0].ssid.push_str("热点");
        assert!(env.validate().is_err());
        for (key, value) in [
            ("rssi", json!(-128)),
            ("rssi", json!(1)),
            ("channel", json!(0)),
            ("channel", json!(15)),
            ("internet", json!("yes")),
            ("ssid", json!("")),
            ("ssid", json!("a\0b")),
            ("security", json!("wpa2")),
        ] {
            let mut ap = serde_json::to_value(&WifiEnvironment::default()).unwrap();
            ap["access_points"][0][key] = value;
            assert!(WifiEnvironment::from_value(&ap).is_err(), "{key}");
        }
    }
}
