mod mcp;
mod output;
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
#[command(
    version,
    about = "Manage and emulate ListenAI devices without a window system"
)]
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
    /// Serve the Model Context Protocol over standard input/output.
    Mcp,
    /// List device instances.
    List,
    /// Create an independent instance with a new OTP identity.
    Create {
        #[arg(long)]
        board: String,
        #[arg(long)]
        name: Option<String>,
        #[arg(long)]
        lpk: Option<PathBuf>,
    },
    /// Add an existing instance to this library without changing its storage.
    Attach { path: PathBuf },
    /// Show live state and persisted instance identity.
    Status { id: Option<String> },
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
    /// Read or cancel the latest button sequence, rejecting stale identifiers.
    ButtonSequenceStatus {
        id: String,
        sequence: u64,
        #[arg(long)]
        run: String,
        #[arg(long)]
        cancel: bool,
    },
    /// Select a PNG/JPEG/PNM camera image, center-cropped to 640x480; retained until cleared.
    Camera {
        id: String,
        #[arg(required_unless_present = "clear", conflicts_with = "clear")]
        image: Option<PathBuf>,
        /// Remove input; sensor capture waits for another image.
        #[arg(long)]
        clear: bool,
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
    /// Send raw bytes to a firmware UART, through its real FIFO.
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
    /// Enable host network access through the board's logical AP.
    #[arg(long)]
    network: bool,
    /// Enable the native host speaker adapter.
    #[arg(long)]
    audio: bool,
    /// Enable the native microphone adapter (implies --audio).
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
    lisem_core::process::init()?;
    let args = Args::parse();
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
    let presentation = output::Output::for_action(&args.command);
    let mut foreground = None;
    let value = match args.command {
        Action::List => json!(manager.catalog.devices()?),
        Action::Create { board, name, lpk } => manager.call(
            "create",
            json!({"board": board, "name": name, "package": lpk.as_deref().map(absolute).transpose()?}),
        )?,
        Action::Attach { path } => manager.call("attach", json!({"path": absolute(&path)?}))?,
        Action::Status { id: None } => manager.status()?,
        Action::Status { id: Some(id) } => manager.call("inspect", json!({"id":id}))?,
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
        Action::ButtonSequenceStatus { id, sequence, run, cancel } => manager.call(
            if cancel { "button_sequence_cancel" } else { "button_sequence_status" },
            json!({"id":id,"run":run,"sequence":sequence}),
        )?,
        Action::Camera { id, image, .. } => {
            let state = manager.call("inspect", json!({"id":id}))?;
            manager.call("camera", json!({"id":id,"run":state["runtime"]["session"]["output"],"path":image.as_deref().map(absolute).transpose()?}))?
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
        Action::Runtime { .. } | Action::Bridge | Action::Mcp => unreachable!(),
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
