//! Physical board descriptions and the checked QEMU assembly plan.
//! Front-end metadata is derived; it is never a second source of wiring.
use anyhow::{Context, Result, bail, ensure};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::collections::{BTreeMap, BTreeSet};

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Board {
    pub schema_version: u32,
    pub id: String,
    pub name: String,
    pub chip: String,
    pub storage: Storage,
    pub devices: BTreeMap<String, Device>,
    pub nets: BTreeMap<String, Net>,
    #[serde(default)]
    pub links: Vec<Link>,
    #[serde(default)]
    pub inputs: BTreeMap<String, Stimulus>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Storage {
    pub flash_bytes: u64,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Device {
    pub model: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub label: Option<String>,
    #[serde(default = "empty_object")]
    pub parameters: Value,
}

fn empty_object() -> Value {
    json!({})
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Net {
    pub endpoints: Vec<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub pull: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Link {
    pub from: String,
    pub to: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
pub enum Stimulus {
    Digital {
        net: String,
        #[serde(default)]
        description: Option<String>,
        default: bool,
    },
    Voltage {
        net: String,
        #[serde(default)]
        description: Option<String>,
        unit: String,
        range: Range,
        default: u32,
    },
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Range {
    pub min: u32,
    pub max: u32,
}

fn identifier(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 64
        && value
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || b"_-".contains(&c))
}

pub fn pad(value: &str, chip: &str) -> Result<u8> {
    let name = value
        .strip_prefix("chip.P")
        .context("Expected a chip pad endpoint")?;
    let bank = match name.as_bytes().first() {
        Some(b'A') => 0,
        Some(b'B') => 32,
        _ => bail!("Unknown pad bank: {value}"),
    };
    let number: u8 = name[1..].parse().context("Invalid pad number")?;
    ensure!(
        number
            < match (chip, bank) {
                ("venusa", 0) => 32,
                ("venusa", _) => 22,
                (_, 0) => 28,
                _ => 10,
            },
        "Chip pad does not exist: {value}"
    );
    ensure!(
        value == format!("chip.P{}{}", if bank == 0 { 'A' } else { 'B' }, number),
        "Pad names must use their canonical spelling: {value}"
    );
    Ok(bank + number)
}

const EXTIO: &str = "listenai-extio-ch32v003-v1.3";

fn extio_pin(port: &str) -> Option<u8> {
    let text = port.strip_prefix('P')?;
    let bank = match text.as_bytes().first()? {
        b'A' => 0,
        b'C' => 2,
        b'D' => 3,
        _ => return None,
    };
    let bit: u8 = text[1..].parse().ok()?;
    let masks = [6u8, 0, 0x9f, 0x7f];
    if bit >= 8 || masks[bank] & (1 << bit) == 0 {
        return None;
    }
    (port == format!("P{}{}", char::from(b'A' + bank as u8), bit))
        .then_some(64 + bank as u8 * 8 + bit)
}

fn ports(model: &str) -> Result<&'static [&'static str]> {
    Ok(match model {
        "st7789" => &["SCK", "SDI", "CS", "DC", "RESET", "TE"],
        "display-panel" => &[],
        "backlight" => &["EN"],
        "button" => &["OUT"],
        "led" | "audio-amplifier" | "signal-probe" => &["IN"],
        "voltage-divider" => &["IN", "OUT"],
        "gc0328" => &[
            "SCL", "SDA", "MCLK", "PCLK", "HSYNC", "VSYNC", "D0", "D1", "D2", "D3", "D4", "D5",
            "D6", "D7", "PWDN", "RESET",
        ],
        "cst328" => &["SCL", "SDA", "INT"],
        "husb320-ba" => &["SCL", "SDA", "INT_N", "ID"],
        "sd-card-slot" => &["CLK", "CMD", "DAT0", "DAT1", "DAT2", "DAT3", "CD"],
        "resistor-keypad" => &["OUT"],
        EXTIO => &[
            "SCL", "SDA", "PA1", "PA2", "PC0", "PC1", "PC2", "PC3", "PC4", "PC7", "PD0", "PD1",
            "PD2", "PD3", "PD4", "PD5", "PD6",
        ],
        _ => bail!("Unsupported device model: {model}"),
    })
}

impl Board {
    fn net(&self, endpoint: &str) -> Option<(&str, &Net)> {
        self.nets
            .iter()
            .find(|(_, n)| n.endpoints.iter().any(|e| e == endpoint))
            .map(|(name, net)| (name.as_str(), net))
    }

    fn chip_pad(&self, endpoint: &str) -> Result<u8> {
        let (_, net) = self
            .net(endpoint)
            .with_context(|| format!("Unconnected endpoint: {endpoint}"))?;
        let mut pads = Vec::new();
        for endpoint in &net.endpoints {
            if endpoint.starts_with("chip.") {
                pads.push(pad(endpoint, &self.chip)?);
            } else if let Some((id, port)) = endpoint.split_once('.') {
                if self.devices.get(id).is_some_and(|d| d.model == EXTIO) {
                    if let Some(pin) = extio_pin(port) {
                        pads.push(pin);
                    }
                }
            }
        }
        ensure!(
            pads.len() == 1,
            "Endpoint requires exactly one chip or expander pin: {endpoint}"
        );
        Ok(pads[0])
    }

    fn single(&self, model: &str) -> Result<Option<(&str, &Device)>> {
        let matches = self
            .devices
            .iter()
            .filter(|(_, d)| d.model == model)
            .collect::<Vec<_>>();
        ensure!(matches.len() <= 1, "At most one {model} is supported");
        Ok(matches.first().map(|(id, dev)| (id.as_str(), *dev)))
    }

    fn linked(&self, endpoint: &str) -> Result<String> {
        let matches = self
            .links
            .iter()
            .filter(|l| l.to == endpoint)
            .collect::<Vec<_>>();
        ensure!(matches.len() == 1, "Expected one source for {endpoint}");
        Ok(matches[0].from.clone())
    }

    pub fn compile(&self) -> Result<Value> {
        ensure!(self.schema_version == 1, "Unsupported board schema version");
        ensure!(
            identifier(&self.id)
                && (1..=80).contains(&self.name.chars().count())
                && !self.name.chars().any(char::is_control),
            "Invalid board ID or name"
        );
        ensure!(
            matches!(self.chip.as_str(), "ls2684" | "ls2663" | "venusa"),
            "Board assembly is unavailable for chip {}",
            self.chip
        );
        ensure!(
            self.storage.flash_bytes == 16 * 1024 * 1024,
            "This chip backend requires a 16 MiB NOR"
        );
        ensure!(
            self.devices.len() <= 64 && self.nets.len() <= 128 && self.links.len() <= 32,
            "Board description exceeds assembly limits"
        );
        for (id, device) in &self.devices {
            ensure!(identifier(id) && id != "chip", "Invalid device ID: {id}");
            ports(&device.model)?;
            ensure!(
                device.parameters.is_object(),
                "Device parameters must be an object: {id}"
            );
        }
        let mut endpoints = BTreeSet::new();
        for (name, net) in &self.nets {
            ensure!(
                identifier(name) && !net.endpoints.is_empty() && net.endpoints.len() <= 16,
                "Invalid net: {name}"
            );
            ensure!(
                matches!(net.pull.as_deref(), None | Some("up" | "down")),
                "Invalid net pull: {name}"
            );
            for endpoint in &net.endpoints {
                ensure!(
                    endpoints.insert(endpoint),
                    "Endpoint belongs to multiple nets: {endpoint}"
                );
                if endpoint == "chip.SD_CD" {
                } else if endpoint.starts_with("chip.") {
                    pad(endpoint, &self.chip)?;
                } else {
                    let (id, port) = endpoint.split_once('.').context("Malformed endpoint")?;
                    let device = self
                        .devices
                        .get(id)
                        .with_context(|| format!("Unknown device: {id}"))?;
                    ensure!(
                        ports(&device.model)?.contains(&port),
                        "Unknown port: {endpoint}"
                    );
                }
            }
        }
        for (name, net) in &self.nets {
            let mut drivers = 0;
            let mut analog = false;
            let mut digital = false;
            for endpoint in &net.endpoints {
                let (id, port) = endpoint.split_once('.').context("Malformed endpoint")?;
                if id == "chip" {
                    continue;
                }
                let model = self.devices[id].model.as_str();
                let is_analog = matches!(model, "voltage-divider" | "resistor-keypad");
                analog |= is_analog;
                digital |= !is_analog && model != EXTIO;
                drivers += usize::from(matches!(
                    (model, port),
                    ("button" | "resistor-keypad" | "voltage-divider", "OUT")
                        | ("st7789", "TE")
                        | ("cst328", "INT")
                        | ("husb320-ba", "INT_N" | "ID")
                        | (
                            "gc0328",
                            "PCLK"
                                | "HSYNC"
                                | "VSYNC"
                                | "D0"
                                | "D1"
                                | "D2"
                                | "D3"
                                | "D4"
                                | "D5"
                                | "D6"
                                | "D7"
                        )
                ));
            }
            ensure!(drivers <= 1, "Multiple fixed drivers on net {name}");
            ensure!(
                !(analog && digital),
                "Mixed analog and digital ports on net {name}"
            );
        }
        for (name, net) in &self.nets {
            let pins = net
                .endpoints
                .iter()
                .filter(|endpoint| {
                    endpoint.starts_with("chip.P")
                        || endpoint.split_once('.').is_some_and(|(id, port)| {
                            self.devices.get(id).is_some_and(|d| d.model == EXTIO)
                                && extio_pin(port).is_some()
                        })
                })
                .count();
            ensure!(pins <= 1, "Direct GPIO-to-GPIO net is unavailable: {name}");
        }
        let mut link_targets = BTreeSet::new();
        for link in &self.links {
            ensure!(
                link_targets.insert(&link.to),
                "Multiple sources for {}",
                link.to
            );
            let (source, signal) = link.from.split_once('.').context("Invalid link source")?;
            let (target, input) = link.to.split_once('.').context("Invalid link target")?;
            let target = self.devices.get(target).context("Unknown link target")?;
            if source == "chip" {
                ensure!(
                    signal == "usb0.host"
                        && target.model == "husb320-ba"
                        && input == "partner"
                        && self.chip != "venusa",
                    "Unsupported chip resource link"
                );
                continue;
            }
            let source = self.devices.get(source).context("Unknown link source")?;
            ensure!(
                target.model == "display-panel"
                    && matches!(
                        (source.model.as_str(), signal, input),
                        ("st7789", "pixels", "pixels") | ("backlight", "brightness", "brightness")
                    ),
                "Incompatible device link: {} -> {}",
                link.from,
                link.to
            );
        }
        let mut buses: BTreeMap<u8, (u8, u8, BTreeSet<u8>)> = BTreeMap::new();
        for (id, device) in &self.devices {
            let address = match device.model.as_str() {
                "gc0328" => 0x21,
                "cst328" => 0x1d,
                EXTIO => serde_json::from_value::<Extio>(device.parameters.clone())?.address,
                "husb320-ba" => serde_json::from_value::<TypeC>(device.parameters.clone())?.address,
                _ => continue,
            };
            let scl = self.chip_pad(&format!("{id}.SCL"))?;
            let sda = self.chip_pad(&format!("{id}.SDA"))?;
            let bus = match (self.chip.as_str(), scl, sda) {
                ("venusa", 34, 33) => 1,
                ("ls2663" | "ls2684", 39, 38) | ("ls2663" | "ls2684", 23, 22) => 0,
                ("ls2663" | "ls2684", 33, 32) => 1,
                _ => bail!("Unsupported I2C pad route for {id}"),
            };
            let route = buses
                .entry(bus)
                .or_insert_with(|| (scl, sda, BTreeSet::new()));
            ensure!(
                route.0 == scl && route.1 == sda,
                "Shared I2C devices must share physical nets"
            );
            ensure!(
                route.2.insert(address),
                "Duplicate I2C slave address on controller {bus}"
            );
        }
        let mut compiled = json!({"id":self.id,"name":self.name,"chip":self.chip,
            "flash_bytes":self.storage.flash_bytes,"uart_channels":[0,1,2],"audio":self.single("audio-amplifier")?.is_some(),
            "buttons":[],"indicators":[],"inputs":[],"definition":self,
            "runtime":{"machine":"arcs-board","capabilities":{"snapshot":true,"wifi":true,
                "sd":false,"usb_adb":true,"download":true,"signals":true,"events":true,
                "button_sequence":true}},
            "assembly":{"version":1,"chip":self.chip,"buttons":[],"indicators":[]}});
        if self.chip == "venusa" {
            compiled["runtime"] = json!({"available":true,"machine":"venusa-board",
                "capabilities":{"snapshot":true,"wifi":false,"sd":false,"usb_adb":false,
                    "download":false,"signals":true,"events":false,"button_sequence":false,"audio":false}});
        }
        let assembly = self.assemble(&mut compiled)?;
        compiled["assembly"] = assembly;
        crate::signals::definitions(&compiled)?;
        Ok(compiled)
    }

    fn assemble(&self, compiled: &mut Value) -> Result<Value> {
        self.single("cst328")?;
        let mut plan = compiled["assembly"].clone();
        if let Some((id, dev)) = self.single(EXTIO)? {
            let p: Extio = serde_json::from_value(dev.parameters.clone())?;
            ensure!(
                [0x6c, 0x15].contains(&p.address),
                "Invalid EXTIO address strap"
            );
            plan["extio"] = json!({"id":id,"SCL":self.chip_pad(&format!("{id}.SCL"))?,
                "SDA":self.chip_pad(&format!("{id}.SDA"))?,"address":p.address});
        }
        if let Some((id, dev)) = self.single("husb320-ba")? {
            let p: TypeC = serde_json::from_value(dev.parameters.clone())?;
            ensure!(
                [0x31, 0x21].contains(&p.address),
                "Invalid HUSB320 address strap"
            );
            let mut device = json!({"SCL":self.chip_pad(&format!("{id}.SCL"))?,
                "SDA":self.chip_pad(&format!("{id}.SDA"))?,"address":p.address,
                "usb_host":self.links.iter().any(|l| l.to == format!("{id}.partner"))});
            for port in ["INT_N", "ID"] {
                if self.net(&format!("{id}.{port}")).is_some() {
                    device[port] = json!(self.chip_pad(&format!("{id}.{port}"))?);
                }
            }
            plan["typec"] = device;
        }
        let mut pulls = Vec::new();
        for net in self.nets.values() {
            if let Some(pull) = &net.pull {
                for endpoint in &net.endpoints {
                    let is_pin = endpoint.starts_with("chip.P")
                        || endpoint.split_once('.').is_some_and(|(id, port)| {
                            self.devices.get(id).is_some_and(|d| d.model == EXTIO)
                                && extio_pin(port).is_some()
                        });
                    if is_pin {
                        pulls.push(json!({"pad":self.chip_pad(endpoint)?,"level":pull=="up"}));
                    }
                }
            }
        }
        plan["pulls"] = json!(pulls);
        if let Some((screen_id, screen)) = self.single("display-panel")? {
            let (lcd_id, lcd) = self
                .single("st7789")?
                .context("Display panel requires an LCD controller")?;
            let (bl_id, bl) = self
                .single("backlight")?
                .context("Display panel requires a backlight")?;
            ensure!(
                self.linked(&format!("{screen_id}.pixels"))? == format!("{lcd_id}.pixels")
                    && self.linked(&format!("{screen_id}.brightness"))?
                        == format!("{bl_id}.brightness"),
                "Display panel links do not match its controller and backlight"
            );
            let p: Panel = serde_json::from_value(screen.parameters.clone())?;
            ensure!(
                p.viewport.width > 0
                    && p.viewport.height > 0
                    && p.viewport.x + p.viewport.width <= 240
                    && p.viewport.y + p.viewport.height <= 320,
                "Display viewport is outside controller RAM"
            );
            ensure!(
                [0, 90, 180, 270].contains(&p.rotation_clockwise),
                "Invalid display rotation"
            );
            let controller: Controller = serde_json::from_value(lcd.parameters.clone())?;
            let backlight: Active = serde_json::from_value(bl.parameters.clone())?;
            ensure!(backlight.active_level <= 1, "Invalid backlight polarity");
            let (width, height) = if p.rotation_clockwise % 180 == 0 {
                (p.viewport.width, p.viewport.height)
            } else {
                (p.viewport.height, p.viewport.width)
            };
            compiled["screen"] = json!({"width":width,"height":height,
                "label":screen.label.as_deref().unwrap_or("屏幕")});
            let mut pads = BTreeMap::new();
            for port in ["SCK", "SDI", "CS", "DC", "RESET", "TE"] {
                pads.insert(port, self.chip_pad(&format!("{lcd_id}.{port}"))?);
            }
            plan["panel"] = json!({"pads":pads,"width":p.viewport.width,"height":p.viewport.height,
                "x_offset":p.viewport.x,"y_offset":p.viewport.y,"rotation":p.rotation_clockwise,
                "inverted":controller.panel_inverted,"backlight_pad":self.chip_pad(&format!("{bl_id}.EN"))?,
                "backlight_active":backlight.active_level});
        } else {
            ensure!(
                self.single("st7789")?.is_none() && self.single("backlight")?.is_none(),
                "LCD controller and backlight require a display panel"
            );
        }
        if let Some((id, camera)) = self.single("gc0328")? {
            let p: Camera = serde_json::from_value(camera.parameters.clone())?;
            ensure!(
                [0, 90, 180, 270].contains(&p.rotation_clockwise),
                "Invalid camera rotation"
            );
            let mut pads = BTreeMap::new();
            for port in [
                "SCL", "SDA", "MCLK", "PCLK", "HSYNC", "VSYNC", "D0", "D1", "D2", "D3", "D4", "D5",
                "D6", "D7",
            ] {
                pads.insert(port, self.chip_pad(&format!("{id}.{port}"))?);
            }
            for port in ["RESET", "PWDN"] {
                if self.net(&format!("{id}.{port}")).is_some() {
                    pads.insert(port, self.chip_pad(&format!("{id}.{port}"))?);
                }
            }
            plan["camera"] = json!({"pads":pads});
            compiled["camera"] = json!({"model":"gc0328","width":640,"height":480,
                "label":camera.label.as_deref().unwrap_or("摄像头"),"rotation_clockwise":p.rotation_clockwise});
        }
        for (id, device) in &self.devices {
            match device.model.as_str() {
                "button" => {
                    let p: Button = serde_json::from_value(device.parameters.clone())?;
                    ensure!(
                        p.pressed_level <= 1
                            && p.released_level <= 1
                            && p.pressed_level != p.released_level,
                        "Invalid button polarity"
                    );
                    let pad = self.chip_pad(&format!("{id}.OUT"))?;
                    compiled["buttons"].as_array_mut().unwrap().push(json!({"id":id,
                        "label":device.label.as_deref().unwrap_or(id),"bank":if pad < 32 {"A"} else {"B"},
                        "pin":pad%32,"endpoint":pad,"active_low":p.pressed_level==0}));
                    let index = plan["buttons"].as_array().unwrap().len();
                    plan["buttons"]
                        .as_array_mut()
                        .unwrap()
                        .push(json!({"id":id,"pad":pad,
                        "pressed_level":p.pressed_level,"released_level":p.released_level}));
                    compiled["runtime"]["buttons"][id] = json!(format!("x-lisa-button-{index}"));
                }
                "led" | "audio-amplifier" | "signal-probe" => {
                    let p: Indicator = serde_json::from_value(device.parameters.clone())?;
                    ensure!(p.active_level <= 1, "Invalid indicator polarity");
                    let pad = self.chip_pad(&format!("{id}.IN"))?;
                    compiled["indicators"].as_array_mut().unwrap().push(json!({"id":id,
                        "label":device.label.as_deref().unwrap_or(id),"kind":if device.model=="signal-probe" {"signal"} else {&device.model},
                        "bank":if pad<32 {"A"} else {"B"},"pin":pad%32,"active_low":p.active_level==0,
                        "color":p.color}));
                    if pad >= 64 {
                        let pin = pad - 64;
                        let row = compiled["indicators"]
                            .as_array_mut()
                            .unwrap()
                            .last_mut()
                            .unwrap();
                        row["device"] = json!(self.single(EXTIO)?.unwrap().0);
                        row["bank"] = json!(char::from(b'A' + pin / 8).to_string());
                        row["pin"] = json!(pin % 8);
                    }
                    if device.model == "audio-amplifier" {
                        ensure!(
                            plan["amplifier"].is_null(),
                            "Only one amplifier is supported"
                        );
                        plan["amplifier"] = json!({"pad":pad,"active_level":p.active_level});
                    }
                }
                "resistor-keypad" => {
                    ensure!(
                        self.single("resistor-keypad")?.is_some() && self.chip != "venusa",
                        "This chip has no keypad ADC adapter"
                    );
                    let p: Keypad = serde_json::from_value(device.parameters.clone())?;
                    let pad = self.chip_pad(&format!("{id}.OUT"))?;
                    let channel = adc_channel(&self.chip, pad)?;
                    ensure!(
                        p.supply_mv <= 3600
                            && p.supply_mv > 0
                            && p.pull_up_ohms > 0
                            && p.pull_up_ohms <= 1_000_000
                            && !p.keys.is_empty(),
                        "Invalid analog keypad"
                    );
                    plan["keypad"] = json!({"channel":channel,"supply_uv":p.supply_mv*1000,"pull_up_ohms":p.pull_up_ohms});
                    for key in p.keys {
                        ensure!(
                            identifier(&key.id) && key.resistance_ohms <= 1_000_000,
                            "Invalid keypad key"
                        );
                        let index = plan["buttons"].as_array().unwrap().len();
                        compiled["buttons"]
                            .as_array_mut()
                            .unwrap()
                            .push(json!({"id":key.id,
                            "label":key.label,"bank":if pad<32 {"A"} else {"B"},"pin":pad%32,
                            "kind":"adc","channel":channel,"active_low":true}));
                        plan["buttons"]
                            .as_array_mut()
                            .unwrap()
                            .push(json!({"id":key.id,"pad":pad,
                            "pressed_level":0,"released_level":1,"resistance_ohms":key.resistance_ohms}));
                        compiled["runtime"]["buttons"][&key.id] =
                            json!(format!("x-lisa-button-{index}"));
                    }
                }
                "cst328" => {
                    let p: Touch = serde_json::from_value(device.parameters.clone())?;
                    ensure!(
                        [0, 90, 180, 270].contains(&p.rotation_clockwise),
                        "Invalid touch rotation"
                    );
                    let scl = self.chip_pad(&format!("{id}.SCL"))?;
                    let sda = self.chip_pad(&format!("{id}.SDA"))?;
                    let irq = self.chip_pad(&format!("{id}.INT"))?;
                    plan["touch"] = json!({"SCL":scl,"SDA":sda,"INT":irq});
                    compiled["touch"] = json!({"model":"cst328","address":"0x1d","width":240,
                        "height":320,"rotation_clockwise":p.rotation_clockwise,
                        "label":device.label.as_deref().unwrap_or("触摸屏")});
                    compiled["runtime"]["touch_property"] = json!("x-lisa-touch");
                }
                _ => {}
            }
        }
        let mut button_ids = BTreeSet::new();
        for b in plan["buttons"].as_array().unwrap() {
            ensure!(
                button_ids.insert(b["id"].as_str().unwrap()),
                "Duplicate button ID"
            );
        }
        let ids: Vec<_> = plan["buttons"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v["id"].clone())
            .collect();
        ensure!(ids.len() <= 16, "At most 16 buttons are supported");
        compiled["runtime"]["button_sequence_ids"] = json!(ids);
        if ids.is_empty() {
            compiled["runtime"]["capabilities"]["button_sequence"] = json!(false);
        }
        if let Some((id, slot)) = self.single("sd-card-slot")? {
            let parameters: Slot = serde_json::from_value(slot.parameters.clone())?;
            ensure!(
                parameters.detect_active_level <= 1,
                "Invalid SD detect polarity"
            );
            let mut pads = BTreeMap::new();
            for port in ["CLK", "CMD", "DAT0", "DAT1", "DAT2", "DAT3"] {
                pads.insert(port, self.chip_pad(&format!("{id}.{port}"))?);
            }
            let expected = if self.chip == "venusa" {
                [42, 43, 44, 45, 46, 47]
            } else {
                [6, 7, 5, 4, 9, 8]
            };
            for (port, expected) in ["CLK", "CMD", "DAT0", "DAT1", "DAT2", "DAT3"]
                .iter()
                .zip(expected)
            {
                ensure!(
                    pads[port] == expected,
                    "Unsupported SD physical pad route: {port}"
                );
            }
            plan["sd"] = json!({"pads":pads,"card_detect":false});
            if let Some((_, net)) = self.net(&format!("{id}.CD")) {
                if net.endpoints.len() == 2 && net.endpoints.iter().any(|e| e == "chip.SD_CD") {
                    ensure!(
                        self.chip != "venusa",
                        "VenusA SD controller detection input is unavailable"
                    );
                    plan["sd"]["card_detect"] = json!(true);
                } else {
                    let endpoint = self.chip_pad(&format!("{id}.CD"))?;
                    plan["sd"]["detect_endpoint"] = json!(endpoint);
                    plan["sd"]["detect_active_level"] = json!(parameters.detect_active_level);
                }
            }
            compiled["runtime"]["capabilities"]["sd"] = json!(true);
        }
        for (id, input) in &self.inputs {
            let input_id = id;
            ensure!(identifier(id) && !id.contains('-'), "Invalid input ID");
            let row = match input {
                Stimulus::Digital {
                    net,
                    description,
                    default,
                } => {
                    let n = self.nets.get(net).context("Unknown input net")?;
                    ensure!(
                        n.endpoints.len() == 1,
                        "Digital stimulus requires one unowned input pin"
                    );
                    let pin = self.chip_pad(&n.endpoints[0])?;
                    json!({"id":id,"label":net,"description":description,"type":"digital",
                        "pin":pin,"endpoint":n.endpoints[0],"default":default})
                }
                Stimulus::Voltage {
                    net,
                    description,
                    unit,
                    range,
                    default,
                } => {
                    ensure!(
                        unit == "mV" && range.min == 0 && *default <= range.max,
                        "Invalid voltage range or unit"
                    );
                    let n = self.nets.get(net).context("Unknown input net")?;
                    ensure!(
                        n.endpoints.len() == 1,
                        "Voltage stimulus requires one analog input endpoint"
                    );
                    let (id, port) = n.endpoints[0]
                        .split_once('.')
                        .context("Invalid analog endpoint")?;
                    let (pin, numerator, denominator, label) = if self
                        .devices
                        .get(id)
                        .is_some_and(|d| d.model == "voltage-divider")
                        && port == "IN"
                    {
                        let p: Divider =
                            serde_json::from_value(self.devices[id].parameters.clone())?;
                        (
                            self.chip_pad(&format!("{id}.OUT"))?,
                            p.numerator,
                            p.denominator,
                            self.net(&format!("{id}.OUT")).unwrap().0,
                        )
                    } else {
                        (self.chip_pad(&n.endpoints[0])?, 1, 1, net.as_str())
                    };
                    let (channel, device) = if pin < 64 {
                        (adc_channel(&self.chip, pin)?, None)
                    } else {
                        let pins = [66, 65, 84, 90, 91, 93, 94, 92];
                        (
                            pins.iter()
                                .position(|p| *p == pin)
                                .context("EXTIO pin has no ADC route")?
                                as u8,
                            Some(self.single(EXTIO)?.unwrap().0),
                        )
                    };
                    let mut row = json!({"id":id,"label":label,"description":description,"type":"voltage",
                        "channel":channel,"default":default,"max":range.max,
                        "divider_numerator":numerator,"divider_denominator":denominator});
                    row["id"] = json!(input_id);
                    if let Some(device) = device {
                        row["device"] = json!(device);
                    }
                    row
                }
            };
            compiled["inputs"].as_array_mut().unwrap().push(row);
        }
        // No net may silently disappear from the assembly plan.
        let used_models = [
            "st7789",
            "backlight",
            "button",
            "led",
            "signal-probe",
            "audio-amplifier",
            "gc0328",
            "sd-card-slot",
            "voltage-divider",
            "resistor-keypad",
            "cst328",
            "husb320-ba",
            EXTIO,
        ];
        for net in self.nets.values() {
            ensure!(
                net.pull.is_some()
                    || net.endpoints.iter().any(|e| !e.starts_with("chip."))
                    || self.inputs.values().any(|i| match i {
                        Stimulus::Digital { net: n, .. } | Stimulus::Voltage { net: n, .. } =>
                            self.nets.get(n).is_some_and(|v| std::ptr::eq(v, net)),
                    }),
                "A net without a device or stimulus has no modeled receiver"
            );
        }
        for d in self.devices.values() {
            ensure!(
                used_models.contains(&d.model.as_str()) || d.model == "display-panel",
                "Unassembled device model"
            );
        }
        Ok(plan)
    }
}

fn adc_channel(chip: &str, pad: u8) -> Result<u8> {
    ensure!(chip != "venusa", "VenusA analog pad routing is unavailable");
    Ok(match pad {
        38 => 4,
        39 => 5,
        40 => 6,
        37 => 7,
        36 => 8,
        35 => 9,
        _ => bail!("Pad has no GPADC VIN route"),
    })
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Slot {
    #[serde(default)]
    detect_active_level: u8,
}

fn extio_address() -> u8 {
    0x6c
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Extio {
    #[serde(default = "extio_address")]
    address: u8,
}

fn typec_address() -> u8 {
    0x31
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct TypeC {
    #[serde(default = "typec_address")]
    address: u8,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Keypad {
    supply_mv: u32,
    pull_up_ohms: u32,
    keys: Vec<AnalogKey>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct AnalogKey {
    id: String,
    label: String,
    resistance_ohms: u32,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Touch {
    #[serde(default)]
    rotation_clockwise: u32,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Panel {
    viewport: Viewport,
    #[serde(default)]
    rotation_clockwise: u32,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Viewport {
    x: u32,
    y: u32,
    width: u32,
    height: u32,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Controller {
    #[serde(default)]
    panel_inverted: bool,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Active {
    active_level: u8,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Button {
    pressed_level: u8,
    released_level: u8,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Indicator {
    active_level: u8,
    #[serde(default)]
    color: Option<String>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Camera {
    #[serde(default)]
    rotation_clockwise: u32,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Divider {
    numerator: u32,
    denominator: u32,
}

/// Model ports and parameter examples for clients composing board files.
pub fn models() -> Value {
    let descriptions = [
        ("st7789", json!({"panel_inverted":true})),
        (
            "display-panel",
            json!({"viewport":{"x":0,"y":0,"width":240,"height":240},"rotation_clockwise":90}),
        ),
        ("backlight", json!({"active_level":1})),
        ("gc0328", json!({"rotation_clockwise":90})),
        ("cst328", json!({"rotation_clockwise":270})),
        ("button", json!({"pressed_level":0,"released_level":1})),
        (
            "resistor-keypad",
            json!({"supply_mv":3300,"pull_up_ohms":10000,
            "keys":[{"id":"key","label":"按键","resistance_ohms":5100}]}),
        ),
        ("led", json!({"active_level":0,"color":"#4cc3a2"})),
        ("audio-amplifier", json!({"active_level":1})),
        ("signal-probe", json!({"active_level":1})),
        ("voltage-divider", json!({"numerator":2,"denominator":5})),
        ("sd-card-slot", json!({"detect_active_level":0})),
        (EXTIO, json!({"address":108})),
        ("husb320-ba", json!({"address":49})),
    ];
    json!(descriptions.into_iter().map(|(model, parameters)|{
        let links = match model {
            "st7789"=>json!({"outputs":["pixels"]}),
            "backlight"=>json!({"outputs":["brightness"]}),
            "display-panel"=>json!({"inputs":["pixels","brightness"]}),
            "husb320-ba"=>json!({"inputs":["partner"],"sources":["chip.usb0.host"]}),
            _=>json!({}),
        };
        json!({"model":model,"ports":ports(model).unwrap(),"parameter_example":parameters,"links":links})
    }).collect::<Vec<_>>())
}

pub fn compile(value: &Value) -> Result<Value> {
    serde_json::from_value::<Board>(value.clone())?.compile()
}

pub fn source(value: &Value) -> &Value {
    value.get("definition").unwrap_or(value)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn example() -> Value {
        serde_json::from_str(include_str!("../../../examples/boards/mini-custom.json")).unwrap()
    }

    #[test]
    fn projection_follows_physical_nets() {
        let mut raw = example();
        raw["nets"]["LCD_DC"]["endpoints"][0] = json!("chip.PA0");
        raw["nets"]["POWER_KEY"]["endpoints"][0] = json!("chip.PB8");
        let board = compile(&raw).unwrap();
        assert_eq!(board["assembly"]["panel"]["pads"]["DC"], 0);
        assert_eq!(board["assembly"]["buttons"][0]["pad"], 40);
        assert_eq!(board["buttons"][0]["pin"], 8);
        assert_eq!(board["screen"]["width"], 240);
        assert_eq!(board["runtime"]["buttons"]["function"], "x-lisa-button-0");
        assert_eq!(
            crate::signals::physical(&board, &Value::Null).unwrap()["adc_uv"]["7"],
            1_680_000
        );
        assert_eq!(source(&board), &raw);
    }

    #[test]
    fn unknown_models_ports_and_duplicate_wires_are_errors() {
        let mut raw = example();
        raw["devices"]["lcd"]["model"] = json!("unknown-screen");
        assert!(
            compile(&raw)
                .unwrap_err()
                .to_string()
                .contains("Unsupported device model")
        );
        let mut raw = example();
        raw["nets"]["LCD_DC"]["endpoints"][1] = json!("lcd.UNKNOWN");
        assert!(compile(&raw).is_err());
        let mut raw = example();
        raw["nets"]["LCD_DC"]["endpoints"][0] = json!("chip.PA25");
        assert!(compile(&raw).is_err());
        let mut raw = example();
        raw["nets"]["LCD_DC"]["endpoints"][0] = json!("chip.PA31");
        assert!(compile(&raw).is_err());
        let mut raw = example();
        raw["devices"]["backlight"]["parameters"]["active_level"] = json!(2);
        assert!(compile(&raw).is_err());
    }

    #[test]
    fn builtins_share_assembly_and_extio_ports_are_independent_of_soc() {
        for (raw, machine) in [
            (include_str!("../../../boards/arcs-mini.json"), "arcs-board"),
            (
                include_str!("../../../boards/arcs-mini3.json"),
                "arcs-board",
            ),
            (include_str!("../../../boards/arcs-evb.json"), "arcs-board"),
            (
                include_str!("../../../boards/venusa-rd-evb.json"),
                "venusa-board",
            ),
        ] {
            let board = compile(&serde_json::from_str(raw).unwrap()).unwrap();
            assert_eq!(board["runtime"]["machine"], machine);
            assert!(board["definition"].is_object());
        }
        let board = compile(
            &serde_json::from_str(include_str!("../../../boards/arcs-mini3.json")).unwrap(),
        )
        .unwrap();
        assert_eq!(board["assembly"]["extio"]["address"], 108);
        assert_eq!(board["assembly"]["typec"]["address"], 49);
        assert_eq!(board["assembly"]["typec"]["usb_host"], true);
        assert_eq!(board["assembly"]["amplifier"]["pad"], 82);
        assert_eq!(board["assembly"]["camera"]["pads"]["PWDN"], 94);
        assert_eq!(board["assembly"]["sd"]["detect_endpoint"], 91);
        let mini: Value =
            serde_json::from_str(include_str!("../../../boards/arcs-mini.json")).unwrap();
        assert_eq!(source(&board)["inputs"], mini["inputs"]);
        let mut conflicting = source(&board).clone();
        conflicting["devices"]["typec"]["parameters"]["address"] = json!(33);
        assert!(
            compile(&conflicting)
                .unwrap_err()
                .to_string()
                .contains("Duplicate I2C")
        );
    }

    #[test]
    fn imported_board_edits_preserve_storage_and_reject_live_changes() {
        let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("board.json");
        let mut raw = example();
        crate::storage::write_json(&path, &raw).unwrap();
        let mut catalog =
            crate::catalog::Catalog::open(&root, &temp.path().join("library")).unwrap();
        catalog.import_board(&path).unwrap();
        let item = catalog.create("mini-custom", None, Some("custom")).unwrap();
        let instance = Path::new(item["path"].as_str().unwrap());
        let flash = crate::storage::sha256(&instance.join("flash.bin")).unwrap();
        let otp = crate::storage::sha256(&instance.join("otp.bin")).unwrap();
        let uid = item["uid"].clone();
        raw["nets"]["LCD_DC"]["endpoints"][0] = json!("chip.PA0");
        crate::storage::write_json(&path, &raw).unwrap();
        let lease =
            crate::storage::locked(instance, &catalog.layout("mini-custom").unwrap()).unwrap();
        assert!(catalog.import_board(&path).is_err());
        drop(lease);
        catalog.import_board(&path).unwrap();
        let next = catalog.device(item["id"].as_str().unwrap()).unwrap();
        assert_eq!(
            next["hardware"]["board"]["assembly"]["panel"]["pads"]["DC"],
            0
        );
        assert_eq!(next["uid"], uid);
        assert_eq!(
            crate::storage::sha256(&instance.join("flash.bin")).unwrap(),
            flash
        );
        assert_eq!(
            crate::storage::sha256(&instance.join("otp.bin")).unwrap(),
            otp
        );
        raw["storage"]["flash_bytes"] = json!(8 * 1024 * 1024);
        crate::storage::write_json(&path, &raw).unwrap();
        assert!(catalog.import_board(&path).is_err());
    }

    #[test]
    fn invalid_reload_retains_the_last_complete_hardware_catalog() {
        let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
        let temp = tempfile::tempdir().unwrap();
        let mut catalog = crate::catalog::Catalog::open(&root, temp.path()).unwrap();
        let before = (catalog.boards.clone(), catalog.chips.clone());
        let custom = temp.path().join("boards/broken.json");
        std::fs::create_dir_all(custom.parent().unwrap()).unwrap();
        crate::storage::write_json(&custom, &json!({"schema_version":1,"id":"broken"})).unwrap();
        assert!(catalog.reload_hardware().is_err());
        assert_eq!((catalog.boards.clone(), catalog.chips.clone()), before);
        std::fs::remove_file(custom).unwrap();
        catalog.reload_hardware().unwrap();
    }

    use std::path::Path;
}
