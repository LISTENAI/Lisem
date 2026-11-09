//! Physical board descriptions and the checked QEMU assembly plan.
//! Front-end metadata is derived; it is never a second source of wiring.
use anyhow::{Context, Result, bail, ensure};
use schemars::JsonSchema;
use serde::{Deserialize, Serialize};

use serde_json::{Value, json};
use std::collections::{BTreeMap, BTreeSet};

mod models;
pub use models::{ModelQuery, query_models, wiring};

#[derive(Clone, Debug, Deserialize, Serialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct Board {
    pub schema_version: u32,
    pub id: String,
    pub name: String,
    pub chip: String,
    #[serde(default)]
    pub storage: Storage,
    #[schemars(length(max = 64))]
    pub devices: BTreeMap<String, Device>,
    #[schemars(length(max = 128))]
    pub nets: BTreeMap<String, Net>,
    #[serde(default)]
    #[schemars(length(max = 32))]
    pub links: Vec<Link>,
    #[serde(default)]
    #[schemars(length(max = 64))]
    pub inputs: BTreeMap<String, Stimulus>,
}

#[derive(Clone, Debug, Default, Deserialize, Serialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct Storage {
    #[serde(default, skip_serializing_if = "is_zero")]
    pub flash_bytes: u64,
}

#[derive(Clone, Debug, Deserialize, Serialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct Device {
    pub model: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub label: Option<String>,
    #[serde(default = "empty_object")]
    pub parameters: Value,
}

fn is_zero(value: &u64) -> bool { *value == 0 }

fn empty_object() -> Value {
    json!({})
}

#[derive(Clone, Debug, Deserialize, Serialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct Net {
    #[schemars(length(min = 1, max = 16))]
    pub endpoints: Vec<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub pull: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct Link {
    pub from: String,
    pub to: String,
}

#[derive(Clone, Debug, Deserialize, Serialize, JsonSchema)]
#[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
pub enum Stimulus {
    Illuminance {
        device: String,
        #[serde(default)]
        description: Option<String>,
        unit: String,
        range: Range,
        default: u32,
    },
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

#[derive(Clone, Debug, Deserialize, Serialize, JsonSchema)]
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

const SUPPORTED_CHIPS: [&str; 3] = ["ls2663", "ls2684", "venusa"];
const EXTIO_ADC_PINS: [u8; 8] = [66, 65, 84, 90, 91, 93, 94, 92];
const NOR_BYTES: u64 = 16 * 1024 * 1024;
const I2C_ROUTES: &[(&str, u8, u8, u8)] = &[
    ("venusa", 34, 33, 1),
    ("ls2663", 39, 38, 0),
    ("ls2663", 23, 22, 0),
    ("ls2663", 33, 32, 1),
    ("ls2684", 39, 38, 0),
    ("ls2684", 23, 22, 0),
    ("ls2684", 33, 32, 1),
];

fn sd_pads(chip: &str) -> [u8; 6] {
    if chip == "venusa" {
        [42, 43, 44, 45, 46, 47]
    } else {
        [6, 7, 5, 4, 9, 8]
    }
}

const ROTATIONS: [u32; 4] = [0, 90, 180, 270];
const EXTIO_ADDRESSES: [u8; 2] = [0x6c, 0x15];
const TYPEC_ADDRESSES: [u8; 2] = [0x31, 0x21];

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
    Ok(models::find(model)?.ports)
}

impl Board {
    fn flash_devices(&self) -> Result<Vec<crate::storage::FlashDevice>> {
        use crate::storage::FlashDevice;
        let mut devices = Vec::new();
        for (id, device) in &self.devices {
            if device.model != "spi-nor" { continue; }
            let p: SerialNor = serde_json::from_value(device.parameters.clone())?;
            ensure!(p.capacity_bytes.is_power_of_two()
                && (65536..=16777216).contains(&p.capacity_bytes),
                "NOR capacity must be a power of two from 64 KiB through 16 MiB");
            ensure!(p.chip_select <= 1 && (self.chip != "venusa" || p.chip_select == 0),
                "Unsupported NOR chip select");
            let bus = self.linked(&format!("{id}.bus"))?;
            ensure!(bus == "chip.flash.data",
                "NOR requires a serial memory controller link");
            if p.chip_select == 1 {
                ensure!(self.chip_pad(&format!("{id}.CS"))? == 11, "Unsupported SPIB CS1 pad route");
            } else {
                ensure!(!self.nets.values().any(|n| n.endpoints.contains(&format!("{id}.CS"))),
                    "Primary NOR uses the controller's dedicated chip select");
            }
            devices.push(FlashDevice::new(id, p.chip_select, p.capacity_bytes));
        }
        if devices.is_empty() {
            ensure!(self.storage.flash_bytes == NOR_BYTES,
                "Board must declare its NOR devices");
            devices.push(FlashDevice::new("flash0", 0, NOR_BYTES as usize));
        }
        devices.sort_by_key(|v| v.chip_select);
        ensure!(devices.len() <= 2 && devices.iter().enumerate().all(|(i,v)| v.chip_select as usize == i),
            "NOR chip selects must be unique and start at CS0");
        Ok(devices)
    }

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
            SUPPORTED_CHIPS.contains(&self.chip.as_str()),
            "Board assembly is unavailable for chip {}",
            self.chip
        );
        let flash_devices = self.flash_devices()?;
        let flash_bytes: u64 = flash_devices.iter().map(|v| v.capacity_bytes as u64).sum();
        ensure!(self.storage.flash_bytes == 0 || self.storage.flash_bytes == flash_bytes,
                "Storage size must match the declared NOR devices");
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
                let is_analog = models::analog(model);
                analog |= is_analog;
                digital |= !is_analog && model != EXTIO;
                drivers += usize::from(models::fixed_output(model, port));
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
                    (signal == "flash.data" && target.model == "spi-nor" && input == "bus")
                        || (self.chip != "venusa"
                        && ((signal == "usb0.host"
                            && target.model == "husb320-ba"
                            && input == "partner")

                            || (signal == "codec.dac"
                                && target.model == "fs1815cn-v1"
                                && input == "analog"))),
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
                "ls8500" => 0x54,
                "gc0328" => 0x21,
                "cst328" => 0x1d,
                "fs1815cn-v1" => 0x34,
                EXTIO => serde_json::from_value::<Extio>(device.parameters.clone())?.address,
                "husb320-ba" => serde_json::from_value::<TypeC>(device.parameters.clone())?.address,
                _ => continue,
            };
            let scl = self.chip_pad(&format!("{id}.SCL"))?;
            let sda = self.chip_pad(&format!("{id}.SDA"))?;
            let bus = I2C_ROUTES
                .iter()
                .find(|(chip, clock, data, _)| *chip == self.chip && *clock == scl && *data == sda)
                .map(|(_, _, _, bus)| *bus)
                .with_context(|| format!("Unsupported I2C pad route for {id}"))?;
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
            "flash_bytes":flash_bytes,"flash_devices":flash_devices,"uart_channels":[0,1,2],"audio":self.single("audio-amplifier")?.is_some() || self.single("fs1815cn-v1")?.is_some(),
            "buttons":[],"rotaries":[],"indicators":[],"inputs":[],"definition":self,
            "runtime":{"machine":"arcs-board","capabilities":{"snapshot":true,"wifi":true,
                "sd":false,"usb_adb":true,"download":true,"signals":true,"signal_trace":true,"events":true,
                "button_sequence":true}},
                "assembly":{"version":1,"chip":self.chip,"buttons":[],"rotaries":[],"indicators":[]}});
        if self.chip == "venusa" {
            compiled["runtime"] = json!({"available":true,"machine":"venusa-board",
                "capabilities":{"snapshot":true,"wifi":false,"sd":false,"usb_adb":false,
                    "download":false,"signals":true,"signal_trace":true,"events":false,"button_sequence":false,"audio":false}});
        }
        let assembly = self.assemble(&mut compiled)?;
        compiled["assembly"] = assembly;
        crate::signals::definitions(&compiled)?;
        Ok(compiled)
    }

    fn assemble(&self, compiled: &mut Value) -> Result<Value> {
        self.single("cst328")?;
        let mut plan = compiled["assembly"].clone();
        plan["nor"] = json!(self.flash_devices()?.iter().map(|v| json!({
            "id":v.id,"chip_select":v.chip_select,"capacity_bytes":v.capacity_bytes
        })).collect::<Vec<_>>());
        if let Some((id, dev)) = self.single(EXTIO)? {
            let p: Extio = serde_json::from_value(dev.parameters.clone())?;
            ensure!(
                EXTIO_ADDRESSES.contains(&p.address),
                "Invalid EXTIO address strap"
            );
            plan["extio"] = json!({"id":id,"SCL":self.chip_pad(&format!("{id}.SCL"))?,
                "SDA":self.chip_pad(&format!("{id}.SDA"))?,"address":p.address});
        }
        if let Some((id, dev)) = self.single("husb320-ba")? {
            let p: TypeC = serde_json::from_value(dev.parameters.clone())?;
            ensure!(
                TYPEC_ADDRESSES.contains(&p.address),
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
                    && p.viewport
                        .x
                        .checked_add(p.viewport.width)
                        .is_some_and(|end| end <= 240)
                    && p.viewport
                        .y
                        .checked_add(p.viewport.height)
                        .is_some_and(|end| end <= 320),
                "Display viewport is outside controller RAM"
            );
            ensure!(
                ROTATIONS.contains(&p.rotation_clockwise),
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
            plan["panel"] = json!({"id":screen_id,"controller_id":lcd_id,"backlight_id":bl_id,
                "pads":pads,"width":p.viewport.width,"height":p.viewport.height,
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
                ROTATIONS.contains(&p.rotation_clockwise),
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
                "ls8500" => {
                    let _: Nor = serde_json::from_value(device.parameters.clone())?;
                    ensure!(self.chip != "venusa" && self.single("ls8500")?.is_some(),
                        "LS8500 requires an LS26 chip and one light sensor");
                    ensure!(self.chip_pad(&format!("{id}.INT"))? < 64, "LS8500 interrupt requires a chip pad");
                    plan["ls8500"] = json!({"id":id,
                        "SCL":self.chip_pad(&format!("{id}.SCL"))?,
                        "SDA":self.chip_pad(&format!("{id}.SDA"))?,
                        "INT":self.chip_pad(&format!("{id}.INT"))?});
                }
                "pwm-led" => {
                    let p: Indicator = serde_json::from_value(device.parameters.clone())?;
                    let pad = self.chip_pad(&format!("{id}.IN"))?;
                    ensure!(self.chip != "venusa" && [0, 1, 21].contains(&pad), "Unsupported PWM LED pad route");
                    ensure!(p.active_level <= 1, "Invalid indicator polarity");
                    if plan["pwm_leds"].is_null() { plan["pwm_leds"] = json!([]); }
                    let rows = plan["pwm_leds"].as_array_mut().unwrap();
                    ensure!(rows.len() < 8, "At most 8 PWM LEDs are supported");
                    rows.push(json!({"id":id,"pad":pad,"active_level":p.active_level}));
                    compiled["indicators"].as_array_mut().unwrap().push(json!({"id":id,
                        "label":device.label.as_deref().unwrap_or(id),"kind":"led","color":p.color,
                        "device":id,"property":"enabled","duty_property":"effective_duty"}));
                }
                "fs1815cn-v1" => {
                    let _: Nor = serde_json::from_value(device.parameters.clone())?;
                    ensure!(self.chip != "venusa" && self.single("fs1815cn-v1")?.is_some(),
                        "FS1815 requires an LS26 chip and one amplifier");
                    ensure!(self.single("audio-amplifier")?.is_none(), "Only one amplifier is supported");
                    ensure!(self.links.iter().any(|link| link.from == "chip.codec.dac"
                        && link.to == format!("{id}.analog")), "FS1815 analog input is not connected");
                    plan["fs1815"] = json!({"id":id,
                        "SCL":self.chip_pad(&format!("{id}.SCL"))?,
                        "SDA":self.chip_pad(&format!("{id}.SDA"))?});
                    compiled["indicators"].as_array_mut().unwrap().push(json!({"id":id,
                        "label":device.label.as_deref().unwrap_or(id),"kind":"audio-amplifier",
                        "device":id,"property":"enabled"}));
                }
                "quadrature-encoder" => {
                    let p: Rotary = serde_json::from_value(device.parameters.clone())?;
                    ensure!([2, 4].contains(&p.edges_per_detent), "Encoder requires 2 or 4 edges per detent");
                    let a = self.chip_pad(&format!("{id}.A"))?;
                    let b = self.chip_pad(&format!("{id}.B"))?;
                    ensure!(a < 64 && b < 64 && a != b, "Encoder requires two distinct chip pads");
                    let rows = plan["rotaries"].as_array_mut().unwrap();
                    ensure!(rows.len() < 4, "At most 4 rotary encoders are supported");
                    rows.push(json!({"id":id,"A":a,"B":b,"edges_per_detent":p.edges_per_detent}));
                    compiled["rotaries"].as_array_mut().unwrap().push(json!({"id":id,
                        "label":device.label.as_deref().unwrap_or(id),"edges_per_detent":p.edges_per_detent}));
                    compiled["runtime"]["capabilities"]["rotary"] = json!(true);
                }
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
                        ROTATIONS.contains(&p.rotation_clockwise),
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
            let expected = sd_pads(&self.chip);
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
                Stimulus::Illuminance { device, description, unit, range, default } => {
                    ensure!(unit == "lux" && range.min == 0 && range.max > 0
                        && range.max <= 60000 && *default <= range.max,
                        "Invalid illuminance range or unit");
                    let sensor = self.devices.get(device).context("Unknown light sensor")?;
                    ensure!(sensor.model == "ls8500", "Device has no illuminance input");
                    json!({"id":id,"label":sensor.label.as_deref().unwrap_or(device),
                        "description":description,"type":"illuminance","device":device,
                        "default":default,"max":range.max})
                }
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
                        (
                            EXTIO_ADC_PINS
                                .iter()
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
            "spi-nor",
            "st7789",
            "backlight",
            "button",
            "quadrature-encoder",
            "led",
            "pwm-led",
            "ls8500",
            "signal-probe",
            "audio-amplifier",
            "fs1815cn-v1",
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
                        Stimulus::Illuminance { .. } => false,
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
    // LS26 User Manual v1.0, Appendix ana_iomux: PB2..PB7 -> VIN0..VIN5.
    // GPADC result channels 0..3 are internal sources, so VIN0 starts at 4.
    Ok(match pad {
        34..=39 => pad - 30,
        _ => bail!("Pad has no GPADC VIN route"),
    })
}

#[derive(Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
struct Nor {}

#[derive(Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
struct SerialNor {
    #[schemars(range(min = 65536, max = 16777216))]
    capacity_bytes: usize,
    #[schemars(range(max = 1))]
    chip_select: u8,
}

#[derive(Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
struct Slot {
    #[serde(default)]
    #[schemars(range(max = 1))]
    detect_active_level: u8,
}

fn extio_address() -> u8 {
    0x6c
}
#[derive(Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
struct Extio {
    #[serde(default = "extio_address")]
    #[schemars(schema_with = "models::extio_address_schema")]
    address: u8,
}

fn typec_address() -> u8 {
    0x31
}
#[derive(Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
struct TypeC {
    #[serde(default = "typec_address")]
    #[schemars(schema_with = "models::typec_address_schema")]
    address: u8,
}

#[derive(Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
struct Keypad {
    /// Supply voltage in millivolts.
    #[schemars(range(min = 1, max = 3600))]
    supply_mv: u32,
    #[schemars(range(min = 1, max = 1000000))]
    pull_up_ohms: u32,
    #[schemars(length(min = 1, max = 16))]
    keys: Vec<AnalogKey>,
}
#[derive(Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
struct AnalogKey {
    id: String,
    label: String,
    #[schemars(range(max = 1000000))]
    resistance_ohms: u32,
}
#[derive(Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
struct Touch {
    #[serde(default)]
    #[schemars(schema_with = "models::rotation_schema")]
    rotation_clockwise: u32,
}

#[derive(Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
struct Panel {
    viewport: Viewport,
    #[serde(default)]
    #[schemars(schema_with = "models::rotation_schema")]
    rotation_clockwise: u32,
}
#[derive(Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
struct Viewport {
    #[schemars(range(max = 239))]
    x: u32,
    #[schemars(range(max = 319))]
    y: u32,
    #[schemars(range(min = 1, max = 240))]
    width: u32,
    #[schemars(range(min = 1, max = 320))]
    height: u32,
}
#[derive(Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
struct Controller {
    #[serde(default)]
    panel_inverted: bool,
}
#[derive(Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
struct Active {
    #[schemars(range(max = 1))]
    active_level: u8,
}
#[derive(Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
struct Button {
    #[schemars(range(max = 1))]
    pressed_level: u8,
    #[schemars(range(max = 1))]
    released_level: u8,
}
#[derive(Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
struct Rotary {
    #[schemars(schema_with = "models::rotary_edges_schema")]
    edges_per_detent: u8,
}
#[derive(Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
struct Indicator {
    #[schemars(range(max = 1))]
    active_level: u8,
    #[serde(default)]
    color: Option<String>,
}
#[derive(Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
struct Camera {
    #[serde(default)]
    #[schemars(schema_with = "models::rotation_schema")]
    rotation_clockwise: u32,
}
#[derive(Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
struct Divider {
    #[schemars(range(min = 1, max = 1000000))]
    numerator: u32,
    #[schemars(range(min = 1, max = 1000000))]
    denominator: u32,
}

/// Model ports and parameters for clients composing board files.
pub fn models() -> Value {
    models::descriptions()
}

pub fn schema() -> Value {
    let mut schema = serde_json::to_value(schemars::schema_for!(Board)).unwrap();
    schema["properties"]["schema_version"]["const"] = json!(1);
    schema["properties"]["chip"]["enum"] = json!(SUPPORTED_CHIPS);
    models::apply_parameter_schemas(&mut schema);
    schema
}

pub fn compile(value: &Value) -> Result<Value> {
    serde_json::from_value::<Board>(value.clone())?.compile()
}

pub(crate) fn flash_devices(value: &Value) -> Result<Vec<crate::storage::FlashDevice>> {
    let board: Board = serde_json::from_value(source(value).clone())?;
    board.flash_devices()
}

pub fn source(value: &Value) -> &Value {
    value.get("definition").unwrap_or(value)
}

/// Digital pad identities and their declared board-net connections.
pub fn trace_sources(compiled: &Value) -> Result<Value> {
    let board: Board = serde_json::from_value(source(compiled).clone())?;
    let mut pins = BTreeMap::new();
    for bank in ['A', 'B'] {
        for pin in 0..32 {
            let id = format!("chip.P{bank}{pin}");
            if let Ok(endpoint) = pad(&id, &board.chip) { pins.insert(id, endpoint); }
        }
    }
    for (id, device) in &board.devices {
        if device.model == EXTIO {
            for port in ports(EXTIO)? {
                if let Some(endpoint) = extio_pin(port) {
                    pins.insert(format!("{id}.{port}"), endpoint);
                }
            }
        }
    }
    Ok(json!(pins.into_iter().map(|(id, endpoint)| {
        let nets: Vec<_> = board.nets.iter().filter(|(_, net)| net.endpoints.contains(&id))
            .map(|(name, _)| name.clone()).collect();
        let connections: BTreeSet<_> = board.nets.values()
            .filter(|net| net.endpoints.contains(&id))
            .flat_map(|net| net.endpoints.iter().filter(|other| **other != id).cloned()).collect();
        json!({"id":id,"endpoint":endpoint,"type":"digital","nets":nets,"connections":connections})
    }).collect::<Vec<_>>()))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn example() -> Value {
        serde_json::from_str(include_str!("../../../examples/boards/mini-custom.json")).unwrap()
    }

    #[test]
    fn illuminance_reaches_one_optical_sensor_and_rejects_invalid_sources() {
        let mut board = json!({"schema_version":1,"id":"light-test","name":"Light",
            "chip":"ls2684","storage":{"flash_bytes":16777216},
            "devices":{"ambient":{"model":"ls8500","parameters":{}}},
            "nets":{"scl":{"endpoints":["chip.PB1","ambient.SCL"]},
                    "sda":{"endpoints":["chip.PB0","ambient.SDA"]},
                    "irq":{"endpoints":["chip.PB2","ambient.INT"]}},
            "inputs":{"light":{"type":"illuminance","device":"ambient","unit":"lux",
                "range":{"min":0,"max":60000},"default":550}}});
        let compiled = compile(&board).unwrap();
        assert_eq!(compiled["assembly"]["ls8500"], json!({"id":"ambient","SCL":33,"SDA":32,"INT":34}));
        assert_eq!(crate::signals::physical(&compiled, &Value::Null).unwrap()["illuminance_lux"]["ambient"], 550);
        let changed = crate::signals::change(&compiled, &Value::Null, &json!({"light":1100})).unwrap();
        assert_eq!(crate::signals::state(&compiled, &changed).unwrap()[0]["unit"], "lux");
        assert_eq!(crate::signals::physical(&compiled, &changed).unwrap()["illuminance_lux"]["ambient"], 1100);
        for value in [json!(-1),json!(60001),json!(true),json!(1.5)] {
            assert!(crate::signals::change(&compiled, &Value::Null, &json!({"light":value})).is_err());
        }
        let restored = crate::signals::change(&compiled, &changed, &json!({"light":null})).unwrap();
        assert_eq!(crate::signals::physical(&compiled, &restored).unwrap()["illuminance_lux"]["ambient"],550);
        board["inputs"]["duplicate"] = board["inputs"]["light"].clone();
        assert!(compile(&board).is_err());
        board["inputs"].as_object_mut().unwrap().remove("duplicate");
        board["inputs"]["light"]["device"] = json!("unknown"); assert!(compile(&board).is_err());
        board["inputs"]["light"]["device"] = json!("ambient");
        board["devices"]["ambient"]["parameters"]["gain"] = json!(2); assert!(compile(&board).is_err());
        board["devices"]["ambient"]["parameters"] = json!({});
        board["nets"].as_object_mut().unwrap().remove("irq"); assert!(compile(&board).is_err());
    }

    #[test]
    fn pwm_led_declares_duty_source_and_checks_physical_route() {
        let mut board = json!({"schema_version":1,"id":"led-test","name":"LED",
            "chip":"ls2684","storage":{"flash_bytes":16777216},
            "devices":{"lamp":{"model":"pwm-led","parameters":{"active_level":1}}},
            "nets":{"drive":{"endpoints":["chip.PA1","lamp.IN"]}}});
        let compiled = compile(&board).unwrap();
        assert_eq!(compiled["assembly"]["pwm_leds"][0], json!({"id":"lamp","pad":1,"active_level":1}));
        assert_eq!(compiled["indicators"][0]["duty_property"], "effective_duty");
        board["nets"]["drive"]["endpoints"][0] = json!("chip.PA2");
        assert!(compile(&board).is_err());
        board["nets"]["drive"]["endpoints"][0] = json!("chip.PA1");
        board["chip"] = json!("venusa");
        assert!(compile(&board).is_err());
    }

    #[test]
    fn fs1815_requires_analog_link_and_unique_amplifier() {
        let mut board = json!({"schema_version":1,"id":"amp-test","name":"Amplifier",
            "chip":"ls2684","storage":{"flash_bytes":16777216},
            "devices":{"speaker":{"model":"fs1815cn-v1","parameters":{}}},
            "nets":{"scl":{"endpoints":["chip.PB1","speaker.SCL"]},
                    "sda":{"endpoints":["chip.PB0","speaker.SDA"]}},
            "links":[{"from":"chip.codec.dac","to":"speaker.analog"}]});
        let compiled = compile(&board).unwrap();
        assert_eq!(compiled["audio"], true);
        assert_eq!(compiled["assembly"]["fs1815"], json!({"id":"speaker","SCL":33,"SDA":32}));
        assert_eq!(compiled["indicators"][0]["property"], "enabled");
        board["links"] = json!([]);
        assert!(compile(&board).is_err());
        board["links"] = json!([{"from":"chip.codec.dac","to":"speaker.analog"}]);
        board["devices"]["speaker"]["parameters"]["gain"] = json!(1);
        assert!(compile(&board).is_err());
        board["devices"]["speaker"]["parameters"] = json!({});
        board["devices"]["amp"] = json!({"model":"audio-amplifier","parameters":{"active_level":1}});
        board["nets"]["amp"] = json!({"endpoints":["chip.PA1","amp.IN"]});
        assert!(compile(&board).is_err());
    }

    #[test]
    fn rotary_requires_two_owned_chip_pads_and_valid_geometry() {
        let valid = json!({"schema_version":1,"id":"rotary-test","name":"Rotary",
            "chip":"ls2684","storage":{"flash_bytes":16777216},
            "devices":{"knob":{"model":"quadrature-encoder","parameters":{"edges_per_detent":2}}},
            "nets":{"A":{"endpoints":["chip.PA4","knob.A"],"pull":"up"},
                "B":{"endpoints":["chip.PA5","knob.B"],"pull":"up"}},"links":[]});
        for chip in ["ls2684", "ls2663", "venusa"] {
            let mut raw = valid.clone(); raw["chip"] = json!(chip);
            let compiled = compile(&raw).unwrap();
            assert_eq!(compiled["runtime"]["capabilities"]["rotary"], true);
            assert_eq!(compiled["assembly"]["rotaries"][0]["A"], 4);
        }
        for (path, value) in [
            ("/devices/knob/parameters/edges_per_detent", json!(3)),
            ("/devices/knob/parameters", json!({"edges_per_detent":2,"unknown":1})),
            ("/nets/B/endpoints/0", json!("chip.PA4")),
            ("/nets/B/endpoints", json!(["knob.B"])),
            ("/nets/B/endpoints/1", json!("knob.C")),
        ] {
            let mut bad = valid.clone(); *bad.pointer_mut(path).unwrap() = value;
            assert!(compile(&bad).is_err(), "accepted {path}");
        }
        let mut bad = valid.clone();
        bad["inputs"] = json!({"conflict":{"type":"digital","net":"A","default":true}});
        assert!(compile(&bad).is_err());
    }

    #[test]
    fn secondary_nor_requires_capacity_bus_and_chip_select() {
        let valid = json!({"schema_version":1,"id":"dual-nor","name":"Dual NOR",
            "chip":"ls2684",
            "devices":{
                "first":{"model":"spi-nor","parameters":{"capacity_bytes":8388608,"chip_select":0}},
                "second":{"model":"spi-nor","parameters":{"capacity_bytes":4194304,"chip_select":1}}},
            "nets":{"CS1":{"endpoints":["chip.PA11","second.CS"]}},
            "links":[{"from":"chip.flash.data","to":"first.bus"},
                     {"from":"chip.flash.data","to":"second.bus"}]});
        assert_eq!(compile(&valid).unwrap()["assembly"]["nor"][1]["capacity_bytes"], 4194304);
        for (path, value) in [
            ("/devices/second/parameters/capacity_bytes", json!(3000000)),
            ("/chip", json!("venusa")),
            ("/nets/CS1/endpoints/0", json!("chip.PA10")),
            ("/links/0/from", json!("chip.unknown")),
            ("/devices/second/parameters", json!({"capacity":8})),
            ("/links", json!([])),
        ] {
            let mut invalid = valid.clone();
            *invalid.pointer_mut(path).unwrap() = value;
            assert!(compile(&invalid).is_err(), "accepted {path}");
        }
    }

    #[test]
    fn trace_sources_follow_installed_board_wiring() {
        let mut raw = example();
        raw["nets"]["LCD_DC"]["endpoints"][0] = json!("chip.PA0");
        let rows = trace_sources(&compile(&raw).unwrap()).unwrap();
        let row = rows.as_array().unwrap().iter().find(|r| r["id"] == "chip.PA0").unwrap();
        assert_eq!(row["endpoint"], 0);
        assert!(row["nets"].as_array().unwrap().contains(&json!("LCD_DC")));
        assert!(row["connections"].as_array().unwrap().contains(&json!("lcd.DC")));
        assert!(!rows.as_array().unwrap().iter().any(|r| r["id"] == "chip.PB31"));
        let mini3: Value = serde_json::from_str(include_str!("../../../boards/arcs-mini3.json")).unwrap();
        let rows = trace_sources(&mini3).unwrap();
        let extio = rows.as_array().unwrap().iter().find(|r| r["id"] == "extio.PC3").unwrap();
        assert_eq!(extio["endpoint"], 83);
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
    fn ls26_analog_pads_follow_vin_channels() {
        for chip in ["ls2663", "ls2684"] {
            for (pin, channel) in [(2, 4), (3, 5), (4, 6), (5, 7), (6, 8), (7, 9)] {
                let raw = json!({"schema_version":1,"id":"analog","name":"Analog",
                    "chip":chip,"storage":{"flash_bytes":16777216},"devices":{},
                    "nets":{"VIN":{"endpoints":[format!("chip.PB{pin}")]}},"links":[],
                    "inputs":{"voltage":{"type":"voltage","net":"VIN","unit":"mV",
                        "range":{"min":0,"max":3300},"default":1680}}});
                let board = compile(&raw).unwrap();
                let physical = crate::signals::physical(&board, &Value::Null).unwrap();
                assert_eq!(physical["adc_uv"][channel.to_string()], 1_680_000);
                assert_eq!(physical["adc_uv"].as_object().unwrap().len(), 1);
            }
            assert!(adc_channel(chip, 33).is_err());
            assert!(adc_channel(chip, 40).is_err());
        }
        assert!(adc_channel("venusa", 39).is_err());
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
    fn invalid_large_viewport_is_rejected_without_arithmetic_overflow() {
        let mut raw = example();
        raw["devices"]["screen"]["parameters"]["viewport"]["x"] = json!(u32::MAX);
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
        let venusa = compile(
            &serde_json::from_str(include_str!("../../../boards/venusa-rd-evb.json")).unwrap(),
        )
        .unwrap();
        assert_eq!(venusa["camera"]["model"], "gc0328");
        assert_eq!(venusa["assembly"]["camera"]["pads"]["SCL"], 34);
        assert_eq!(venusa["assembly"]["camera"]["pads"]["SDA"], 33);
        assert_eq!(venusa["assembly"]["camera"]["pads"]["MCLK"], 35);
        assert_eq!(venusa["assembly"]["camera"]["pads"]["D7"], 31);
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

    #[test]
    fn nor_capacity_is_declared_per_device() {
        let raw = json!({"schema_version":1,"id":"unequal-nor","name":"Unequal NOR",
            "chip":"ls2684", "devices":{
                "boot":{"model":"spi-nor","parameters":{"capacity_bytes":4194304,"chip_select":0}},
                "data":{"model":"spi-nor","parameters":{"capacity_bytes":8388608,"chip_select":1}}},
            "nets":{"CS":{"endpoints":["chip.PA11","data.CS"]}},
            "links":[{"from":"chip.flash.data","to":"boot.bus"},
                     {"from":"chip.flash.data","to":"data.bus"}]});
        let compiled = compile(&raw).unwrap();
        assert_eq!(compiled["flash_bytes"],12582912);
        assert_eq!(compiled["flash_devices"][0]["capacity_bytes"],4194304);
        assert_eq!(compiled["flash_devices"][1]["file"],"flash-cs1.bin");
        assert_eq!(compiled["assembly"]["nor"][1]["capacity_bytes"],8388608);
        for (path,value) in [
            ("/devices/data/parameters/capacity_bytes",json!(0)),
            ("/devices/data/parameters/capacity_bytes",json!(6291456)),
            ("/devices/data/parameters/capacity_bytes",json!(33554432)),
            ("/devices/data/parameters/chip_select",json!(0)),
            ("/devices/data/parameters/chip_select",json!(2)),
            ("/chip",json!("venusa")),
        ] {
            let mut bad = raw.clone(); *bad.pointer_mut(path).unwrap() = value;
            assert!(compile(&bad).is_err(), "accepted {path}");
        }
    }
}
