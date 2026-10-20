//! The board validator and public capability discovery share this registry.
use super::*;

#[derive(Default, Deserialize, Serialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct ModelQuery {
    /// Category from the returned categories list, for example camera.
    pub category: Option<String>,
    /// Exact model ID, case-insensitive. No compatibility guessing.
    pub model: Option<String>,
    /// Include complete descriptions for a category listing.
    #[serde(default)]
    pub details: bool,
    /// Include the board JSON schema, installed chips and reference boards.
    #[serde(default)]
    pub schema: bool,
}

pub(super) struct Model {
    pub id: &'static str,
    category: &'static str,
    description: &'static str,
    pub ports: &'static [&'static str],
}

const MODELS: &[Model] = &[
    Model {
        id: "st7789",
        category: "display",
        description: "SPI display controller with pixel RAM and TE output",
        ports: &["SCK", "SDI", "CS", "DC", "RESET", "TE"],
    },
    Model {
        id: "display-panel",
        category: "display",
        description: "Visible display area and physical installation orientation",
        ports: &[],
    },
    Model {
        id: "backlight",
        category: "display",
        description: "Digital backlight enable and panel brightness source",
        ports: &["EN"],
    },
    Model {
        id: "gc0328",
        category: "camera",
        description: "SCCB-controlled camera sensor with parallel pixel output",
        ports: &[
            "SCL", "SDA", "MCLK", "PCLK", "HSYNC", "VSYNC", "D0", "D1", "D2", "D3", "D4", "D5",
            "D6", "D7", "PWDN", "RESET",
        ],
    },
    Model {
        id: "cst328",
        category: "input",
        description: "I2C touch controller with interrupt output",
        ports: &["SCL", "SDA", "INT"],
    },
    Model {
        id: "button",
        category: "input",
        description: "Independently controlled digital button",
        ports: &["OUT"],
    },
    Model {
        id: "resistor-keypad",
        category: "input",
        description: "Buttons sharing an ADC resistor ladder",
        ports: &["OUT"],
    },
    Model {
        id: "led",
        category: "indicator",
        description: "Digital LED state indicator",
        ports: &["IN"],
    },
    Model {
        id: "signal-probe",
        category: "indicator",
        description: "Digital output state observation",
        ports: &["IN"],
    },
    Model {
        id: "audio-amplifier",
        category: "audio",
        description: "Board audio amplifier enable",
        ports: &["IN"],
    },
    Model {
        id: "voltage-divider",
        category: "analog",
        description: "External voltage scaled into an ADC input",
        ports: &["IN", "OUT"],
    },
    Model {
        id: "sd-card-slot",
        category: "storage",
        description: "SD bus and optional card detection output",
        ports: &["CLK", "CMD", "DAT0", "DAT1", "DAT2", "DAT3", "CD"],
    },
    Model {
        id: EXTIO,
        category: "gpio-expander",
        description: "CH32V003 EXTIO fixed protocol version 1.3",
        ports: &[
            "SCL", "SDA", "PA1", "PA2", "PC0", "PC1", "PC2", "PC3", "PC4", "PC7", "PD0", "PD1",
            "PD2", "PD3", "PD4", "PD5", "PD6",
        ],
    },
    Model {
        id: "husb320-ba",
        category: "usb",
        description: "I2C USB-C role controller and partner detection",
        ports: &["SCL", "SDA", "INT_N", "ID"],
    },
];

pub(super) fn find(id: &str) -> Result<&'static Model> {
    MODELS
        .iter()
        .find(|m| m.id == id)
        .with_context(|| format!("Unsupported device model: {id}"))
}

fn parameter_schema(id: &str) -> Value {
    macro_rules! schema {
        ($ty:ty) => {
            serde_json::to_value(schemars::schema_for!($ty)).unwrap()
        };
    }
    match id {
        "st7789" => schema!(Controller),
        "display-panel" => schema!(Panel),
        "backlight" => schema!(Active),
        "gc0328" => schema!(Camera),
        "cst328" => schema!(Touch),
        "button" => schema!(Button),
        "resistor-keypad" => schema!(Keypad),
        "led" | "signal-probe" | "audio-amplifier" => schema!(Indicator),
        "voltage-divider" => schema!(Divider),
        "sd-card-slot" => schema!(Slot),
        EXTIO => schema!(Extio),
        "husb320-ba" => schema!(TypeC),
        _ => unreachable!(),
    }
}

pub(super) fn apply_parameter_schemas(board: &mut Value) {
    let template = board["$defs"]["Device"].clone();
    let mut alternatives = Vec::new();
    for model in MODELS {
        let mut device = template.clone();
        let mut parameters = parameter_schema(model.id);
        parameters.as_object_mut().unwrap().remove("$schema");
        if let Some(definitions) = parameters.as_object_mut().unwrap().remove("$defs") {
            for (name, definition) in definitions.as_object().unwrap() {
                board["$defs"][name] = definition.clone();
            }
        }
        if parameters["required"]
            .as_array()
            .is_some_and(|fields| !fields.is_empty())
        {
            device["required"]
                .as_array_mut()
                .unwrap()
                .push(json!("parameters"));
        }
        device["properties"]["model"]["const"] = json!(model.id);
        device["properties"]["parameters"] = parameters;
        alternatives.push(device);
    }
    board["$defs"]["Device"] = json!({"oneOf":alternatives});
}

fn example(id: &str) -> Value {
    match id {
        "st7789" => json!({"panel_inverted":true}),
        "display-panel" => {
            json!({"viewport":{"x":0,"y":0,"width":240,"height":240},"rotation_clockwise":90})
        }
        "backlight" | "audio-amplifier" | "signal-probe" => json!({"active_level":1}),
        "gc0328" => json!({"rotation_clockwise":90}),
        "cst328" => json!({"rotation_clockwise":270}),
        "button" => json!({"pressed_level":0,"released_level":1}),
        "resistor-keypad" => {
            json!({"supply_mv":3300,"pull_up_ohms":10000,"keys":[{"id":"key","label":"按键","resistance_ohms":5100}]})
        }
        "led" => json!({"active_level":0,"color":"#4cc3a2"}),
        "voltage-divider" => json!({"numerator":2,"denominator":5}),
        "sd-card-slot" => json!({"detect_active_level":0}),
        EXTIO => json!({"address":extio_address()}),
        "husb320-ba" => json!({"address":typec_address()}),
        _ => unreachable!(),
    }
}

pub(super) fn fixed_output(model: &str, port: &str) -> bool {
    matches!(
        (model, port),
        ("button" | "resistor-keypad" | "voltage-divider", "OUT")
            | ("st7789", "TE")
            | ("cst328", "INT")
            | ("husb320-ba", "INT_N" | "ID")
            | (
                "gc0328",
                "PCLK" | "HSYNC" | "VSYNC" | "D0" | "D1" | "D2" | "D3" | "D4" | "D5" | "D6" | "D7"
            )
    )
}

pub(super) fn analog(model: &str) -> bool {
    matches!(model, "voltage-divider" | "resistor-keypad")
}

fn signals(m: &Model) -> Vec<Value> {
    m.ports
        .iter()
        .map(|port| {
            let direction = if fixed_output(m.id, port) || (m.id == "sd-card-slot" && *port == "CD")
            {
                "output"
            } else if *port == "SDA"
                || (m.id == EXTIO && port.starts_with('P'))
                || (m.id == "sd-card-slot" && *port != "CLK")
            {
                "bidirectional"
            } else {
                "input"
            };
            let kind = if analog(m.id) { "voltage" } else { "digital" };
            let required = !matches!(
                (m.id, *port),
                ("gc0328", "PWDN" | "RESET")
                    | ("husb320-ba", "INT_N" | "ID")
                    | ("sd-card-slot", "CD")
            ) && !(m.id == EXTIO && port.starts_with('P'));
            json!({"name":port,"direction":direction,"kind":kind,"required":required})
        })
        .collect()
}

fn links(id: &str) -> Value {
    match id {
        "st7789" => json!({"outputs":["pixels"]}),
        "backlight" => json!({"outputs":["brightness"]}),
        "display-panel" => json!({"inputs":["pixels","brightness"]}),
        "husb320-ba" => json!({"inputs":["partner"],"sources":["chip.usb0.host"]}),
        _ => json!({}),
    }
}

fn constraints(id: &str) -> Value {
    match id {
        "st7789" | "backlight" | "display-panel" => json!([
            "One controller, one panel and one backlight must be connected together.",
            "Connect controller.pixels to panel.pixels and backlight.brightness to panel.brightness.",
            "Viewport must fit 240x320 controller RAM; width and height must be positive."
        ]),
        "gc0328" => json!([
            "One camera per board; SCCB slave address is fixed at 0x21.",
            "SCL/SDA use a supported chip I2C route; pixel data and clocks use chip camera pads.",
            "PWDN and RESET are optional and may use GPIO expander pins.",
            "Host image or live camera supplies a 640x480 RGB888 scene; firmware configures sensor sampling and capture."
        ]),
        "cst328" => json!(["One touch controller per board; I2C address is fixed at 0x1d."]),
        "button" => json!([
            "Pressed and released levels must differ; at most 16 board buttons including keypad keys."
        ]),
        "resistor-keypad" => json!([
            "One keypad per board; requires a supported chip ADC route.",
            "Key IDs must be unique among all board buttons."
        ]),
        "audio-amplifier" => {
            json!(["One amplifier per board; GPIO enable also gates board speaker feedback."])
        }
        "signal-probe" | "led" => json!([
            "State snapshots expose driven level and active polarity; output edge capture is unavailable."
        ]),
        "voltage-divider" => json!([
            "Output feeds an ADC route; the voltage input is measured before the divider.",
            "Numerator and denominator must be positive and numerator must not exceed denominator."
        ]),
        "sd-card-slot" => json!([
            "One SD slot per board; CLK/CMD/DAT0..3 use the chip's fixed SD routes.",
            "CD is optional; connect to a GPIO/expander or the supported controller detection input.",
            "Card image insertion is independent of a mechanical detection signal."
        ]),
        EXTIO => json!([
            "One expander per board; protocol version is part of the model ID.",
            "SCL/SDA use a chip I2C route; GPIO endpoints can drive modeled controls and observe board inputs."
        ]),
        "husb320-ba" => json!([
            "One role controller per board; partner link accepts chip.usb0.host on supported chips."
        ]),
        _ => json!([]),
    }
}

fn describe(m: &Model, details: bool) -> Value {
    let mut value = json!({"model":m.id,"category":m.category,"description":m.description});
    if details {
        value["ports"] = json!(m.ports);
        value["signals"] = json!(signals(m));
        value["supported_chips"] = if m.id == "resistor-keypad" {
            json!(["ls2663", "ls2684"])
        } else {
            json!(SUPPORTED_CHIPS)
        };
        if m.id == EXTIO {
            value["analog_inputs"] = json!(EXTIO_ADC_PINS.iter().enumerate().map(|(channel, pin)| {
                let pin = pin - 64;
                json!({"port":format!("P{}{}", char::from(b'A' + pin / 8), pin % 8),"channel":channel,"unit":"mV"})
            }).collect::<Vec<_>>());
        }
        value["parameter_schema"] = parameter_schema(m.id);
        value["parameter_example"] = example(m.id);
        value["links"] = links(m.id);
        value["data_signals"] = data_signals(m.id);
        value["interfaces"] = interfaces(m.id);
        value["constraints"] = constraints(m.id);
    }
    value
}

pub(super) fn descriptions() -> Value {
    json!(MODELS.iter().map(|m| describe(m, true)).collect::<Vec<_>>())
}

pub fn query_models(query: &ModelQuery) -> Result<Value> {
    let categories: BTreeSet<_> = MODELS.iter().map(|m| m.category).collect();
    let category = query
        .category
        .as_deref()
        .map(str::trim)
        .map(str::to_ascii_lowercase);
    if let Some(category) = &category {
        ensure!(
            categories.contains(category.as_str()),
            "Unknown model category: {category}; available: {}",
            categories.iter().copied().collect::<Vec<_>>().join(", ")
        );
    }
    let model = query.model.as_deref().map(str::trim);
    ensure!(
        model.is_none_or(|m| !m.is_empty() && m.len() <= 128),
        "Invalid model query"
    );
    let details = query.details || model.is_some() || category.is_none();
    let models: Vec<_> = MODELS
        .iter()
        .filter(|m| {
            category
                .as_ref()
                .is_none_or(|category| m.category == category)
                && model.is_none_or(|id| m.id.eq_ignore_ascii_case(id))
        })
        .map(|m| describe(m, details))
        .collect();
    Ok(
        json!({"categories":categories,"query":{"category":category,"model":model},
        "found":!models.is_empty(),"models":models,"build":crate::identity::build()}),
    )
}

pub(super) fn rotation_schema(_: &mut schemars::SchemaGenerator) -> schemars::Schema {
    schemars::json_schema!({"type":"integer", "enum":ROTATIONS,
        "description":"Physical installation rotation, clockwise degrees."})
}

pub(super) fn extio_address_schema(_: &mut schemars::SchemaGenerator) -> schemars::Schema {
    schemars::json_schema!({"type":"integer", "enum":EXTIO_ADDRESSES,
        "description":"7-bit I2C slave address selected by hardware strap."})
}

pub(super) fn typec_address_schema(_: &mut schemars::SchemaGenerator) -> schemars::Schema {
    schemars::json_schema!({"type":"integer", "enum":TYPEC_ADDRESSES,
        "description":"7-bit I2C slave address selected by hardware strap."})
}

fn data_signals(id: &str) -> Value {
    match id {
        "st7789" => json!([{"name":"pixels","direction":"output","kind":"pixels","required":true}]),
        "backlight" => {
            json!([{"name":"brightness","direction":"output","kind":"brightness","required":true}])
        }
        "display-panel" => json!([
            {"name":"pixels","direction":"input","kind":"pixels","required":true},
            {"name":"brightness","direction":"input","kind":"brightness","required":true}]),
        "husb320-ba" => {
            json!([{"name":"partner","direction":"input","kind":"usb-host-partner","required":false,
            "sources":["chip.usb0.host"],"chips":["ls2663","ls2684"]}])
        }
        _ => json!([]),
    }
}

fn interfaces(id: &str) -> Value {
    let address = match id {
        "gc0328" => Some(0x21),
        "cst328" => Some(0x1d),
        _ => None,
    };
    if let Some(address) = address {
        return json!([{ "protocol":if id == "gc0328" {"sccb"} else {"i2c"},
            "address_7bit":address,"clock":"SCL","data":"SDA" }]);
    }
    match id {
        EXTIO | "husb320-ba" => {
            json!([{"protocol":"i2c","address_parameter":"address","clock":"SCL","data":"SDA"}])
        }
        "st7789" => json!([{"protocol":"spi","clock":"SCK","data":"SDI","select":"CS"}]),
        _ => json!([]),
    }
}

fn endpoint(pin: u8) -> String {
    format!("chip.P{}{}", if pin < 32 { 'A' } else { 'B' }, pin % 32)
}

/// Routing constraints are projections of the same tables used during assembly.
pub fn wiring(chip: &str) -> Value {
    let pads: Vec<_> = (0..64)
        .map(endpoint)
        .filter(|name| pad(name, chip).is_ok())
        .collect();
    let adc: Vec<_> = (0..64)
        .filter_map(|pin| {
            adc_channel(chip, pin)
                .ok()
                .map(|channel| json!({"pin":endpoint(pin),"channel":channel}))
        })
        .collect();
    let i2c: Vec<_> = I2C_ROUTES.iter().filter(|(id, _, _, _)| *id == chip)
        .map(|(_, scl, sda, bus)| json!({"controller":bus,"SCL":endpoint(*scl),"SDA":endpoint(*sda)})).collect();
    let sd: BTreeMap<_, _> = ["CLK", "CMD", "DAT0", "DAT1", "DAT2", "DAT3"]
        .into_iter()
        .zip(sd_pads(chip).into_iter().map(endpoint))
        .collect();
    json!({"pads":pads,"i2c":i2c,"adc":adc,"sd":sd,"flash_bytes":NOR_BYTES,
        "controller_sd_detect":chip!="venusa","usb_host_partner":chip!="venusa",
        "constraints":["Firmware configures controller clocks and PinMux; board JSON describes physical wiring.",
            "Each modeled endpoint belongs to one net; at most one chip or expander GPIO per net.",
            "I2C devices on one controller share physical SCL/SDA nets and use distinct addresses.",
            "Analog stimulus uses millivolts; connect its divider output to a supported ADC input.",
            "Use board-validate for complete routing, resource and cross-parameter checks."]})
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn category_listing_and_exact_lookup_have_distinct_detail_levels() {
        let category = ModelQuery {
            category: Some("Camera".into()),
            ..Default::default()
        };
        let result = query_models(&category).unwrap();
        assert_eq!(result["models"].as_array().unwrap().len(), 1);
        assert_eq!(result["models"][0]["model"], "gc0328");
        assert!(result["models"][0].get("parameter_schema").is_none());
        let exact = query_models(&ModelQuery {
            model: Some("GC0328".into()),
            ..Default::default()
        })
        .unwrap();
        let camera = &exact["models"][0];
        assert_eq!(camera["parameter_schema"]["additionalProperties"], false);
        assert_eq!(
            camera["parameter_schema"]["properties"]["rotation_clockwise"]["enum"],
            json!(ROTATIONS)
        );
        assert!(
            camera["signals"]
                .as_array()
                .unwrap()
                .iter()
                .any(|p| p["name"] == "PCLK"
                    && p["direction"] == "output"
                    && p["required"] == true)
        );
        assert!(
            camera["signals"]
                .as_array()
                .unwrap()
                .iter()
                .any(|p| p["name"] == "PWDN"
                    && p["direction"] == "input"
                    && p["required"] == false)
        );
        let detailed = query_models(&ModelQuery {
            details: true,
            ..category
        })
        .unwrap();
        assert_eq!(detailed["models"], exact["models"]);
    }

    #[test]
    fn missing_models_do_not_guess_compatibility_or_ignore_filters() {
        for query in [
            ModelQuery {
                model: Some("GC0828".into()),
                ..Default::default()
            },
            ModelQuery {
                model: Some("gc0328".into()),
                category: Some("display".into()),
                ..Default::default()
            },
        ] {
            let value = query_models(&query).unwrap();
            assert_eq!(value["found"], false);
            assert!(value["models"].as_array().unwrap().is_empty());
        }
        assert!(
            query_models(&ModelQuery {
                category: Some("not-a-category".into()),
                ..Default::default()
            })
            .is_err()
        );
        assert!(
            query_models(&ModelQuery {
                model: Some(" ".into()),
                ..Default::default()
            })
            .is_err()
        );
    }

    #[test]
    fn installed_board_endpoints_and_routing_are_discoverable() {
        let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
        let temp = tempfile::tempdir().unwrap();
        let mut catalog = crate::catalog::Catalog::open(&root, temp.path()).unwrap();
        let description = catalog
            .model_catalog(&ModelQuery {
                schema: true,
                ..Default::default()
            })
            .unwrap();
        assert!(description["board_schema"]["$defs"]["Stimulus"].is_object());
        for board in description["boards"].as_array().unwrap() {
            let board = super::super::source(board);
            let devices = board["devices"].as_object().unwrap();
            for net in board["nets"].as_object().unwrap().values() {
                for endpoint in net["endpoints"].as_array().unwrap() {
                    let (id, port) = endpoint.as_str().unwrap().split_once('.').unwrap();
                    if id == "chip" {
                        continue;
                    }
                    let model = find(devices[id]["model"].as_str().unwrap()).unwrap();
                    assert!(model.ports.contains(&port));
                }
            }
        }
        let routes = &description["wiring"]["ls2663"];
        assert!(
            routes["i2c"]
                .as_array()
                .unwrap()
                .iter()
                .any(|r| r["SCL"] == "chip.PB7" && r["SDA"] == "chip.PB6" && r["controller"] == 0)
        );
        assert_eq!(routes["sd"]["DAT0"], "chip.PA5");
        assert_eq!(description["wiring"]["venusa"]["usb_host_partner"], false);

        let extio = query_models(&ModelQuery {
            model: Some(EXTIO.into()),
            ..Default::default()
        })
        .unwrap();
        let pc4 = extio["models"][0]["analog_inputs"]
            .as_array()
            .unwrap()
            .iter()
            .find(|pin| pin["port"] == "PC4")
            .unwrap();
        let mut mini3 = description["boards"]
            .as_array()
            .unwrap()
            .iter()
            .find(|b| b["id"] == "arcs-mini3")
            .unwrap()["definition"]
            .clone();
        mini3["nets"]["BAT_VCC_DET"]["endpoints"][1] = json!("extio.PC4");
        let compiled = super::super::compile(&mini3).unwrap();
        let voltage = compiled["inputs"]
            .as_array()
            .unwrap()
            .iter()
            .find(|input| input["id"] == "battery_voltage")
            .unwrap();
        assert_eq!(voltage["channel"], pc4["channel"]);
        assert_eq!(voltage["device"], "extio");
    }
}
