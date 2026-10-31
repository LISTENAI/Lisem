mod audio;
mod uart;
mod mcp;
mod output;
mod python;
use anyhow::{Result, ensure};
use clap::{Parser, Subcommand};
use lisem_core::{
    catalog::Catalog,
    manager::{self, Manager},
    runtime::{self, Client, Options},
    storage,
};
use serde_json::json;
use std::{
    path::{Path, PathBuf},
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    time::{Duration, Instant},
};

#[derive(Parser)]
#[command(version, about = "Manage and emulate ListenAI devices")]
struct Args {
    #[arg(long, global = true)]
    root: Option<PathBuf>,
    #[arg(long, global = true)]
    data_dir: Option<PathBuf>,
    #[arg(long, global = true)]
    json: bool,
    #[command(subcommand)]
    command: Action,
}
#[derive(Subcommand)]
enum Action {
    /// Show the compiled native identity without opening an instance library.
    BuildInfo,
    /// Run a user Python with the SDK bundled with this executable.
    Python {
        /// Interpreter executable; defaults to the active environment, then PATH.
        #[arg(long, conflicts_with="sdk_path")]
        interpreter: Option<PathBuf>,
        /// Print the paired SDK import path; --json also includes source and build identity.
        #[arg(long, conflicts_with="args")]
        sdk_path: bool,
        /// Arguments passed unchanged to Python after --.
        #[arg(last=true)]
        args: Vec<std::ffi::OsString>,
    },
    /// Serve versioned native management RPC over standard input/output.
    Rpc,
    /// Serve the Model Context Protocol over standard input/output.
    Mcp,
    /// List device instances.
    List,
    /// Query device models by category or exact model ID.
    BoardModels {
        /// Category from the returned categories list, for example camera.
        #[arg(long)]
        category: Option<String>,
        /// Exact model ID (case-insensitive); an unknown model returns found=false.
        #[arg(long)]
        model: Option<String>,
        /// Include full descriptions in a category listing.
        #[arg(long)]
        details: bool,
        /// Include board JSON schema, installed chips and reference boards.
        #[arg(long)]
        schema: bool,
    },
    /// Validate a board JSON and show its resolved hardware description.
    BoardValidate { path: PathBuf },
    /// Import or replace a custom board; affected instances must be powered off.
    BoardImport { path: PathBuf },
    /// Create an independent instance with a new OTP identity.
    Create {
        #[arg(long)]
        board: String,
        #[arg(long)]
        name: Option<String>,
        #[arg(long)]
        lpk: Option<PathBuf>,
        /// Exact chip UID in hexadecimal; defaults to a random prefixed UID (prefix: ff00).
        #[arg(long)]
        uid: Option<String>,
        /// Allow the specified UID without the default prefix for this request.
        #[arg(long, requires = "uid")]
        allow_unprefixed_uid: bool,
    },
    /// Add an existing instance to this library without changing its storage.
    Attach { path: PathBuf },
    /// Show live state and persisted instance identity.
    Status { id: Option<String> },
    /// Show or replace nearby Wi-Fi hotspots from a JSON file.
    WifiEnvironment {
        id: String,
        #[arg(long)]
        file: Option<PathBuf>,
        #[arg(long)]
        run: Option<String>,
    },
    /// Write only the ranges specified by a validated LPK.
    Import { id: String, package: PathBuf },
    /// Write a binary into Flash at a byte offset.
    Write {
        id: String,
        image: PathBuf,
        /// Byte offset in decimal or hexadecimal (0x...).
        #[arg(long, default_value_t = 0, value_parser = parse_offset)]
        offset: u64,
    },
    /// Erase all Flash while retaining OTP and UID.
    Erase { id: String },
    /// Show or regenerate the instance UID; regeneration requires power off.
    Uid {
        id: String,
        #[arg(long)]
        regenerate: bool,
    },
    /// Run in the foreground; return guest errors and stop on interruption.
    Run {
        id: String,
        #[command(flatten)]
        options: RunOptions,
    },
    /// Start in the background for subsequent control commands.
    Start {
        id: String,
        #[command(flatten)]
        options: RunOptions,
    },
    /// Power off, preserving enabled UART endpoints.
    Stop { id: String },
    /// Reset, optionally into the chip ROM download mode.
    Reset {
        id: String,
        #[arg(long)]
        download: bool,
    },
    /// Send an explicit press or release of a board button.
    Button {
        id: String,
        button: String,
        #[arg(value_parser=["press","release"])]
        state: String,
    },
    /// Schedule button presses on virtual time; returns the sequence identifier.
    ButtonSequence {
        id: String,
        button: String,
        #[arg(long)]
        run: String,
        #[arg(long, default_value_t = 1)]
        count: u64,
        #[arg(long)]
        hold_ms: u64,
        #[arg(long, default_value_t = 80)]
        gap_ms: u64,
    },
    /// Inspect typed board inputs, or set a JSON object of values (null restores defaults).
    Signals { id: String, #[arg(long)] set: Option<String>, #[arg(long)] run: Option<String> },
    /// List digital sources or record transitions at their virtual event times.
    SignalTrace {
        id: String,
        #[arg(long)] run: Option<String>,
        #[arg(long, num_args=1.., conflicts_with_all=["trace", "stop"])] start: Option<Vec<String>>,
        #[arg(long, requires="start")] capacity: Option<usize>,
        #[arg(long)] trace: Option<String>,
        #[arg(long, requires="trace")] stop: bool,
        #[arg(long, requires="trace", conflicts_with="stop")] after: Option<u64>,
        #[arg(long, requires="trace", conflicts_with="stop")] limit: Option<usize>,
    },
    /// Operate a generic virtual BLE central using a run-bound JSON request.
    Ble {
        id: String,
        #[arg(long)]
        run: String,
        /// Generic BLE request JSON: action, peer, connection and operation fields.
        #[arg(long)]
        request: String,
    },
    /// Inspect or enable a loopback TCP bridge to the firmware USB ADB interface.
    UsbAdb {
        id: String,
        #[arg(long, conflicts_with = "disable")]
        enable: bool,
        #[arg(long)]
        disable: bool,
        #[arg(long)]
        run: Option<String>,
    },
    /// Inspect, mount or eject a raw SD card image.
    SdCard { id: String, #[arg(long, conflicts_with="eject")] image: Option<PathBuf>,
        #[arg(long)] eject: bool, #[arg(long)] run: Option<String> },
    /// Arm a bounded event program from a JSON file.
    #[command(after_long_help = r#"Plan example (future UART 0 bytes 'ready', then an 80 ms press):
  {"trigger":{"uart_hex":"7265616479","channel":0,"timeout_ms":10000},
   "actions":[{"after_ms":250,"kind":"button","button":"function",
               "count":1,"hold_ms":80,"gap_ms":0}]}

Trigger: {"at_ns":N} uses absolute virtual nanoseconds; UART matching observes
future bytes with a virtual timeout. Actions are ordered by after_ms (0..86400000)
relative to the trigger. 1..32 actions; the plan must fit the run budget.
Kinds: button (button,count,hold_ms,gap_ms), audio (absolute WAV path),
access_point (enabled), power_off (last action). Button counts: 1..32;
hold_ms >= 1, gap_ms >= 1 for repeated presses, total sequence <= 60000 ms.
Dispatched means input delivery; confirm child sequence and firmware results."#)]
    Events { id: String, file: PathBuf, #[arg(long)] run: String },
    /// Inspect or cancel an event program.
    EventsStatus { id: String, sequence: u64, #[arg(long)] run: String, #[arg(long)] cancel: bool },
    /// Receive raw UART bytes across power cycles until Ctrl+C or the host deadline.
    UartReceive { id: String, channel: u8, #[arg(long)] capture: PathBuf,
        #[arg(long, default_value_t = 36000)] timeout: u64 },
    /// Read or cancel the latest button sequence, rejecting stale identifiers.
    ButtonSequenceStatus {
        id: String,
        sequence: u64,
        #[arg(long)]
        run: String,
        #[arg(long)]
        cancel: bool,
    },
    /// List host camera IDs and authorization without starting capture.
    CameraDevices,
    /// Select a camera image or host camera; retained until cleared.
    Camera {
        id: String,
        #[arg(required_unless_present_any = ["clear", "device"], conflicts_with_all = ["clear", "device"])]
        image: Option<PathBuf>,
        /// Host camera ID from camera-devices. No automatic fallback.
        #[arg(long, conflicts_with = "clear")]
        device: Option<String>,
        /// Remove any input; sensor capture waits for a new source.
        #[arg(long)]
        clear: bool,
    },
    /// Set Wi-Fi access point availability.
    AccessPoint {
        id: String,
        #[arg(value_parser=["on", "off"])]
        state: String,
        /// Reject a stale run; otherwise use the currently inspected run.
        #[arg(long)]
        run: Option<String>,
    },
    /// Inject a WAV in real time; wait for completion, Ctrl+C cancels input only.
    AudioInput {
        id: String,
        path: PathBuf,
        /// Host deadline, including time waiting for the firmware ADC.
        #[arg(long, value_parser = clap::value_parser!(u64).range(1..=172800))]
        timeout: Option<u64>,
    },
    /// Record raw DAC output to WAV until Ctrl+C; keeps the device running.
    AudioOutput {
        id: String,
        /// New output WAV path. Existing files are never overwritten.
        #[arg(long)]
        capture: PathBuf,
    },
    /// Save the current device display as PNG.
    Screenshot { id: String, output: PathBuf },
    /// Enable or disable a persistent host UART endpoint.
    Uart {
        id: String,
        channel: u8,
        #[arg(long)]
        disable: bool,
    },
    /// Send raw bytes to a firmware UART.
    Send {
        id: String,
        channel: u8,
        text: String,
        #[arg(long)]
        hex: bool,
    },
    /// Read a bounded UART history without consuming the terminal connection.
    Logs {
        id: String,
        #[arg(default_value_t = 0)]
        channel: u8,
        #[arg(long, default_value_t = 0)]
        cursor: u64,
        #[arg(long, default_value_t = 4096)]
        limit: usize,
    },
    /// Power off and close this instance's host endpoints.
    Shutdown { id: String },
    #[command(name = "_runtime", hide = true)]
    Runtime { id: String },
    #[command(name = "_bridge", hide = true)]
    Bridge,
}
#[derive(clap::Args)]
struct RunOptions {
    #[arg(long, default_value_t = 300)]
    seconds: u64,
    #[arg(long, default_value_t = 800)]
    timeout: u64,
    /// Enable host network access through the configured Wi-Fi hotspots.
    #[arg(long)]
    network: bool,
    /// Play device audio through the host speakers.
    #[arg(long)]
    audio: bool,
    /// Capture the host microphone (implies --audio).
    #[arg(long)]
    microphone: bool,
    #[arg(long)]
    mute: bool,
    /// Save diagnostic recordings and traces under a directory.
    #[arg(long)]
    capture: Option<PathBuf>,
}
impl TryFrom<RunOptions> for Options {
    type Error = anyhow::Error;
    fn try_from(v: RunOptions) -> Result<Self> {
        Ok(Self {
            seconds: v.seconds,
            timeout: v.timeout,
            network: v.network,
            host_audio: v.audio || v.microphone,
            microphone: v.microphone,
            sound: !v.mute,
            download: false,
            capture: v.capture.as_deref().map(absolute).transpose()?,
        })
    }
}

fn parse_offset(value: &str) -> std::result::Result<u64, std::num::ParseIntError> {
    match value
        .strip_prefix("0x")
        .or_else(|| value.strip_prefix("0X"))
    {
        Some(hex) => u64::from_str_radix(hex, 16),
        None => value.parse(),
    }
}

fn absolute(path: &Path) -> Result<PathBuf> {
    Ok(std::path::absolute(path)?)
}

fn run() -> Result<i32> {
    let args = Args::parse();
    match args.command {
        Action::BuildInfo => {
            println!("{}", serde_json::to_string(&lisem_core::identity::build())?);
            return Ok(0);
        }
        Action::Python { interpreter, sdk_path, args: python_args } => {
            ensure!(args.root.is_none() && args.data_dir.is_none(), "Set script libraries with Client(data_dir=...); Python always uses its bundled SDK");
            if sdk_path {
                let info = python::sdk_info()?;
                println!("{}", if args.json { serde_json::to_string(&info)? } else { info["path"].as_str().unwrap().to_owned() });
                return Ok(0);
            }
            ensure!(!args.json, "--json applies to Python SDK path queries only");
            return python::run(interpreter, python_args);
        }
        _ => {}
    }
    lisem_core::process::init()?;
    let root = lisem_core::paths::runtime_root(args.root)?;
    let data = args
        .data_dir
        .map(Ok)
        .unwrap_or_else(lisem_core::paths::data_dir)?;
    let stop = Arc::new(AtomicBool::new(false));
    let signal = stop.clone();
    ctrlc::set_handler(move || {
        signal.store(true, Ordering::Release);
    })?;
    if let Action::Runtime { id } = args.command {
        runtime::serve(Catalog::open(&root, &data)?, id, stop)?;
        return Ok(0);
    }
    let mut manager = Manager::new(&root, &data, &std::env::current_exe()?)?;
    if matches!(args.command, Action::Mcp) {
        mcp::serve(manager, stop)?;
        return Ok(0);
    }
    if matches!(args.command, Action::Bridge) {
        manager::bridge(manager)?;
        return Ok(0);
    }
    if matches!(args.command, Action::Rpc) {
        manager::rpc(manager)?;
        return Ok(0);
    }
    let presentation = output::Output::for_action(&args.command);
    let mut foreground = None;
    let value = match args.command {
        Action::Python { .. } | Action::BuildInfo => unreachable!(),
        Action::List => json!(manager.catalog.devices()?),
        Action::BoardModels { category, model, details, schema } => {
            let legacy = category.is_none() && model.is_none() && !details && !schema;
            let result = manager.catalog.model_catalog(&lisem_core::board::ModelQuery {
                category, model, details, schema,
            })?;
            if legacy { result["models"].clone() } else { result }
        },
        Action::BoardValidate { path } => manager.call("board_validate", json!({"path":absolute(&path)?}))?,
        Action::BoardImport { path } => manager.call("board_import", json!({"path":absolute(&path)?}))?,
        Action::Create { board, name, lpk, uid, allow_unprefixed_uid } => manager.call(
            "create",
            json!({"board": board, "name": name, "package": lpk.as_deref().map(absolute).transpose()?, "uid":uid, "allow_unprefixed_uid":allow_unprefixed_uid}),
        )?,
        Action::Attach { path } => manager.call("attach", json!({"path": absolute(&path)?}))?,
        Action::Status { id: None } => manager.status()?,
        Action::Status { id: Some(id) } => manager.call("inspect", json!({"id":id}))?,
        Action::WifiEnvironment { id, file, run } => {
            if let Some(path) = file {
                let environment = storage::read_json(&path)?;
                let current = manager.call("inspect", json!({"id":id}))?;
                let session = &current["runtime"]["session"];
                let run = run.map(serde_json::Value::String).unwrap_or_else(||
                    if session["finished"] == false { session["output"].clone() } else { serde_json::Value::Null });
                manager.call("wifi_environment", json!({"id":id,"run":run,"environment":environment}))?["environment"]["wifi"].clone()
            } else {
                manager.catalog.device(&id)?["environment"]["wifi"].clone()
            }
        }
        Action::Import { id, package } => manager.call(
            "import", json!({"id": id, "package": absolute(&package)?}),
        )?,
        Action::Write { id, image, offset } => manager.call(
            "write_flash", json!({"id": id, "path": absolute(&image)?, "offset": offset}),
        )?,
        Action::Erase { id } => {
            let item = manager.catalog.device(&id)?;
            manager.call("erase", json!({"id": id, "confirm_uid": item["uid"]}))?
        }
        Action::Uid { id, regenerate } => {
            let item = manager.catalog.device(&id)?;
            if regenerate {
                manager.call("regenerate_uid", json!({"id": id, "confirm_uid": item["uid"]}))?["uid"].clone()
            } else {
                item["uid"].clone()
            }
        }
        Action::Run { id, options } => {
            let options = Options::try_from(options)?;
            foreground = Some((id.clone(), options.timeout));
            manager.call("start", json!({"id": id, "options": options}))?
        }
        Action::Start { id, options } => manager.call(
            "start", json!({"id": id, "options": Options::try_from(options)?}),
        )?,
        Action::Stop { id } => manager.call("stop", json!({"id": id}))?,
        Action::Reset { id, download } => manager.call(
            if download { "reset_download" } else { "reset" }, json!({"id": id}),
        )?,
        Action::Button { id, button, state } => manager.call(
            "button", json!({"id": id, "button": button, "pressed": state == "press"}),
        )?,
        Action::ButtonSequence { id, button, run, count, hold_ms, gap_ms } => manager.call(
            "button_sequence", json!({"id":id,"button":button,"run":run,"count":count,"hold_ms":hold_ms,"gap_ms":gap_ms}),
        )?,
        Action::Signals { id, set, run } => {
            let mut params = json!({"id":id,"run":run});
            if let Some(values) = set {
                if params["run"].is_null() {
                    let state = manager.call("inspect", json!({"id":id}))?;
                    if state["runtime"]["session"]["finished"] == false {
                        params["run"] = state["runtime"]["session"]["output"].clone();
                    }
                }
                params["changes"] = serde_json::from_str(&values)?;
            }
            manager.call("signals", params)?
        }
        Action::SignalTrace { id, run, start, capacity, trace, stop, after, limit } => {
            use lisem_core::signal_trace::{Action as TraceAction, Request};
            let action = if start.is_some() { TraceAction::Start }
                else if stop { TraceAction::Stop }
                else if trace.is_some() { TraceAction::Read } else { TraceAction::List };
            let request = Request { id, run, action, capture: trace, sources: start, capacity, after, limit };
            request.validate()?;
            manager.call("signal_trace", serde_json::to_value(request)?)?
        }
        Action::Ble { id, run, request } => {
            let mut params: serde_json::Value = serde_json::from_str(&request)?;
            anyhow::ensure!(params.is_object(), "BLE request must be a JSON object");
            params["id"] = json!(id);
            params["run"] = json!(run);
            manager.call("ble", params)?
        }
        Action::UsbAdb { id, enable, disable, run } => {
            let mut params = json!({"id":id,"run":run});
            if enable || disable {
                if params["run"].is_null() {
                    let state = manager.call("inspect", json!({"id":id}))?;
                    params["run"] = state["runtime"]["session"]["output"].clone();
                }
                params["enabled"] = json!(enable);
            }
            manager.call("usb_adb", params)?
        }
        Action::SdCard { id, image, eject, run } => {
            let mut params = json!({"id":id});
            if image.is_some() || eject {
                let state = manager.call("inspect", json!({"id":id}))?;
                params["run"] = run.map(serde_json::Value::String).unwrap_or_else(||
                    if state["runtime"]["session"]["finished"] == false { state["runtime"]["session"]["output"].clone() }
                    else { serde_json::Value::Null });
                params["medium"] = match image { Some(path) => json!({"path":absolute(&path)?}), None => serde_json::Value::Null };
            }
            manager.call("sd_card", params)?
        }
        Action::Events { id, file, run } => manager.call("events", json!({"id":id,"run":run,
            "action":"arm","plan":storage::read_json(&file)?}))?,
        Action::EventsStatus { id, sequence, run, cancel } => manager.call("events", json!({"id":id,"run":run,
            "sequence":sequence,"action":if cancel {"cancel"} else {"status"}}))?,
        Action::UartReceive { id, channel, capture, timeout } => uart::receive(&mut manager, &id, channel, &absolute(&capture)?, timeout, &stop)?,
        Action::ButtonSequenceStatus { id, sequence, run, cancel } => manager.call(
            if cancel { "button_sequence_cancel" } else { "button_sequence_status" },
            json!({"id":id,"run":run,"sequence":sequence}),
        )?,
        Action::AccessPoint { id, state, run } => {
            let current = manager.call("inspect", json!({"id":id}))?;
            let running = current["runtime"]["session"]["finished"] == false;
            let run = run.map(serde_json::Value::String).unwrap_or_else(|| if running {
                current["runtime"]["session"]["output"].clone()
            } else { serde_json::Value::Null });
            manager.call("access_point", json!({"id":id,"run":run,"enabled":state == "on"}))?
        }
        Action::AudioInput { id, path, timeout } => audio::input(&mut manager, &id, &absolute(&path)?, timeout, &stop)?,
        Action::AudioOutput { id, capture } => audio::output(&mut manager, &id, &absolute(&capture)?, &stop)?,
        Action::CameraDevices => manager.call("camera_devices", json!({}))?,
        Action::Camera { id, image, device, .. } => {
            let state = manager.call("inspect", json!({"id":id}))?;
            let mut params = json!({"id":id,"run":state["runtime"]["session"]["output"]});
            if let Some(device) = device {
                ensure!(!device.trim().is_empty(), "Camera device ID must not be empty");
                params["device_id"] = json!(device);
            } else {
                params["path"] = json!(image.as_deref().map(absolute).transpose()?);
            }
            manager.call("camera", params)?
        }
        Action::Screenshot { id, output } => manager.call(
            "screenshot", json!({"id": id, "path": absolute(&output)?}),
        )?,
        Action::Uart { id, channel, disable } => manager.call(
            "serial", json!({"id": id, "channel": channel, "enabled": !disable}),
        )?,
        Action::Send { id, channel, text, hex } => manager.call(
            "uart_write",
            json!({"id": id, "channel": channel, "hex": if hex { text } else { storage::hex(text.as_bytes()) }}),
        )?,
        Action::Logs { id, channel, cursor, limit } => manager.call("uart_read", json!({"id":id,"channel":channel,"cursor":cursor,"limit":limit}))?,
        Action::Shutdown { id } => manager.call("shutdown", json!({"id": id}))?,
        Action::Runtime { .. } | Action::Bridge | Action::Mcp | Action::Rpc => unreachable!(),
    };
    if let Some((id, timeout)) = foreground {
        let item = manager.catalog.device(&id)?;
        let client = Client::connect(Path::new(item["path"].as_str().unwrap()))?;
        let deadline = Instant::now() + Duration::from_secs(timeout + 45);
        loop {
            if stop.load(Ordering::Acquire) {
                client.call("stop", json!({}))?;
                return Ok(130);
            }
            let state = client.call("status", json!({}))?;
            if state["session"]["finished"] == true {
                println!(
                    "{}",
                    if args.json {
                        serde_json::to_string(&state)?
                    } else {
                        presentation.render(&state)
                    }
                );
                ensure!(
                    state["session"]["error"].is_null() && state["session"]["guest_fault"] != true,
                    "Guest run failed; inspect the reported error"
                );
                return Ok(0);
            }
            ensure!(
                Instant::now() < deadline,
                "Runtime did not finish within its host budget"
            );
            std::thread::sleep(Duration::from_millis(100));
        }
    }
    manager.disown();
    if !args.json && matches!(presentation, output::Output::Raw) {
        use std::io::Write;
        if value["lost"] == true {
            eprintln!("Earlier UART bytes are no longer in the memory buffer.");
        }
        std::io::stdout().write_all(&storage::unhex(value["hex"].as_str().unwrap_or(""))?)?;
        return Ok(0);
    }
    println!(
        "{}",
        if args.json {
            serde_json::to_string(&value)?
        } else {
            presentation.render(&value)
        }
    );
    Ok(0)
}
fn main() {
    match run() {
        Ok(code) => std::process::exit(code),
        Err(error) => {
            eprintln!("{error:#}");
            std::process::exit(1);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn python_arguments_follow_an_explicit_boundary() {
        let parsed = Args::try_parse_from(["lisem","python","--interpreter","path with spaces/python","--","-m","pytest","-h","--interpreter","test-value"]).unwrap();
        let Action::Python { interpreter, sdk_path, args } = parsed.command else { panic!() };
        assert_eq!(interpreter, Some(PathBuf::from("path with spaces/python")));
        assert!(!sdk_path);
        assert_eq!(args, ["-m","pytest","-h","--interpreter","test-value"].map(std::ffi::OsString::from));
        assert!(Args::try_parse_from(["lisem","python","script.py"]).is_err());
        assert!(Args::try_parse_from(["lisem","python","--sdk-path","--","-c","pass"]).is_err());
        assert!(Args::try_parse_from(["lisem","python","--sdk-path","--interpreter","python"]).is_err());
        assert!(Args::try_parse_from(["lisem","--json","python","--sdk-path"]).is_ok());
    }

    #[test]
    fn uid_override_permission_requires_a_uid() {
        assert!(Args::try_parse_from(["lisem","create","--board","arcs-mini","--allow-unprefixed-uid"]).is_err());
        assert!(Args::try_parse_from(["lisem","create","--board","arcs-mini","--uid","0102a37c102d9e65","--allow-unprefixed-uid"]).is_ok());
    }

    #[test]
    fn usb_adb_arguments_select_one_operation() {
        for args in [
            vec!["lisem", "usb-adb", "instance"],
            vec!["lisem", "usb-adb", "instance", "--enable"],
            vec!["lisem", "usb-adb", "instance", "--disable", "--run", "run-id"],
        ] {
            assert!(Args::try_parse_from(args).is_ok());
        }
        assert!(Args::try_parse_from([
            "lisem", "usb-adb", "instance", "--enable", "--disable"
        ]).is_err());
    }

    #[test]
    fn camera_source_arguments_are_exclusive() {
        for args in [
            vec!["lisem", "camera-devices"],
            vec!["lisem", "camera", "instance", "image.png"],
            vec!["lisem", "camera", "instance", "--device", "camera-id"],
            vec!["lisem", "camera", "instance", "--clear"],
        ] {
            assert!(Args::try_parse_from(args).is_ok());
        }
        for args in [
            vec!["lisem", "camera", "instance"],
            vec!["lisem", "camera", "instance", "image.png", "--clear"],
            vec![
                "lisem",
                "camera",
                "instance",
                "image.png",
                "--device",
                "camera-id",
            ],
            vec![
                "lisem",
                "camera",
                "instance",
                "--device",
                "camera-id",
                "--clear",
            ],
        ] {
            assert!(Args::try_parse_from(args).is_err());
        }
    }
}
