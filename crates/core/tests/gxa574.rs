use serde_json::{Value, json};

fn board() -> Value {
    json!({"schema_version":1,"id":"expander-test","name":"Expander","chip":"ls2684","storage":{"flash_bytes":16777216},
 "devices":{"io":{"model":"gxa574","parameters":{"address":32}}},
 "nets":{"SCL":{"endpoints":["chip.PA20","io.SCL"]},"SDA":{"endpoints":["chip.PA21","io.SDA"]}},"links":[]})
}

#[test]
fn gxa574_led_does_not_panic() {
    let mut b = board();
    b["devices"]["led"] = json!({"model":"led","parameters":{"active_level":0}});
    b["nets"]["LED"] = json!({"endpoints":["io.P1","led.IN"]});
    assert!(lisem_core::board::compile(&b).is_ok());
}

#[test]
fn duplicate_expanders_are_rejected() {
    let mut b = board();
    b["devices"]["io2"] = json!({"model":"gxa574","parameters":{"address":32}});
    b["nets"]["SCL2"] = json!({"endpoints":["chip.PB1","io2.SCL"]});
    b["nets"]["SDA2"] = json!({"endpoints":["chip.PB0","io2.SDA"]});
    let r = lisem_core::board::compile(&b);
    assert!(
        r.is_err(),
        "silently accepted second expander: {:?}",
        r.unwrap()["assembly"]["extio"]
    );
}

#[test]
fn gxa574_pull_is_preserved() {
    let mut b = board();
    b["nets"]["INPUT"] = json!({"endpoints":["io.P0"],"pull":"down"});
    let c = lisem_core::board::compile(&b).unwrap();
    assert!(
        c["assembly"]["pulls"]
            .as_array()
            .unwrap()
            .iter()
            .any(|p| p["pad"] == 64 && p["level"] == false),
        "{:?}",
        c["assembly"]["pulls"]
    );
}

#[test]
fn gxa574_defaults_and_unsupported_capabilities() {
    let mut b = board();
    b["devices"]["io"]["parameters"] = json!({});
    assert_eq!(
        lisem_core::board::compile(&b).unwrap()["assembly"]["extio"]["address"],
        32
    );
    for value in [
        json!({"address":33}),
        json!({"address":64}),
        json!({"protocol":1}),
    ] {
        b["devices"]["io"]["parameters"] = value;
        assert!(lisem_core::board::compile(&b).is_err());
    }
    for pin in ["P8", "PA0", "INT_N", "ADC0"] {
        let mut invalid = board();
        invalid["nets"]["UNSUPPORTED"] = json!({"endpoints":[format!("io.{pin}")]});
        assert!(
            lisem_core::board::compile(&invalid).is_err(),
            "accepted {pin}"
        );
    }
}

#[test]
fn mixed_expanders_are_rejected() {
    let mut b = board();
    b["devices"]["io2"] = json!({"model":"listenai-extio-ch32v003-v1.3","parameters":{}});
    b["nets"]["SCL"]["endpoints"]
        .as_array_mut()
        .unwrap()
        .push(json!("io2.SCL"));
    b["nets"]["SDA"]["endpoints"]
        .as_array_mut()
        .unwrap()
        .push(json!("io2.SDA"));
    assert!(lisem_core::board::compile(&b).is_err());
}

#[test]
fn gxa574_i2c_routes_match_the_controller() {
    let mut b = board();
    b["nets"]["SCL"]["endpoints"][0] = json!("chip.PA19");
    b["nets"]["SDA"]["endpoints"][0] = json!("chip.PA18");
    assert!(lisem_core::board::compile(&b).is_ok());
    b["nets"]["SCL"]["endpoints"][0] = json!("chip.PA18");
    b["nets"]["SDA"]["endpoints"][0] = json!("chip.PA17");
    assert!(lisem_core::board::compile(&b).is_err());
}
