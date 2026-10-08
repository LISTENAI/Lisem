//! MCP is a transport over the same instance manager used by the CLI and GUI.
use anyhow::{Context, Result, ensure};
use lisem_core::{manager::Manager, runtime::Options, storage};
use rmcp::{
    ErrorData, RoleServer, ServerHandler, ServiceExt,
    model::*,
    schemars::{self, JsonSchema},
    service::RequestContext,
};
use serde::{Deserialize, Serialize, de::DeserializeOwned};
use serde_json::{Value, json};
use std::{
    path::PathBuf,
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
    time::Duration,
};

#[derive(Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
struct Empty {}
#[derive(Deserialize, Serialize, JsonSchema)]
#[serde(deny_unknown_fields)]
struct Instance {
    id: String,
}
#[derive(Deserialize, Serialize, JsonSchema)]
#[serde(deny_unknown_fields)]
struct Run {
    id: String,
    /// Run identifier returned as runtime.session.output by lisem_status.
    run: String,
}
#[derive(Deserialize, Serialize, JsonSchema)]
#[serde(deny_unknown_fields)]
struct Create {
    board: String,
    name: Option<String>,
    /// LPK path on the machine running Lisem.
    package: Option<PathBuf>,
}
#[derive(Deserialize, Serialize, JsonSchema)]
#[serde(deny_unknown_fields)]
struct Import {
    id: String,
    package: PathBuf,
}
#[derive(Deserialize, Serialize, JsonSchema)]
#[serde(deny_unknown_fields)]
struct Flash {
    id: String,
    path: PathBuf,
    #[serde(default)]
    offset: u64,
}
#[derive(Deserialize, Serialize, JsonSchema)]
#[serde(deny_unknown_fields)]
struct Confirm {
    id: String,
    /// Current UID, obtained from lisem_status; prevents targeting a changed identity.
    confirm_uid: String,
}
#[derive(Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
struct Start {
    id: String,
    /// Virtual seconds, 5..600; defaults to 300.
    seconds: Option<u64>,
    /// Host deadline in seconds, 1..850; defaults to 800.
    timeout: Option<u64>,
    #[serde(default)]
    network: bool,
    #[serde(default)]
    audio: bool,
    #[serde(default)]
    microphone: bool,
    #[serde(default)]
    mute: bool,
    /// Boot the chip ROM in download mode.
    #[serde(default)]
    download: bool,
}
#[derive(Deserialize, Serialize, JsonSchema)]
#[serde(deny_unknown_fields)]
struct Reset {
    id: String,
    run: String,
    #[serde(default)]
    download: bool,
}
#[derive(Deserialize, Serialize, JsonSchema)]
#[serde(deny_unknown_fields)]
struct Button {
    id: String,
    run: String,
    button: String,
    pressed: bool,
}
#[derive(Deserialize, Serialize, JsonSchema)]
#[serde(deny_unknown_fields)]
struct Uart {
    id: String,
    channel: u8,
    enabled: bool,
}
#[derive(Deserialize, Serialize, JsonSchema)]
#[serde(deny_unknown_fields)]
struct ReadUart {
    id: String,
    run: String,
    channel: u8,
    #[serde(default)]
    cursor: u64,
    /// Maximum bytes, 1..16384; defaults to 4096.
    #[serde(default = "read_limit")]
    limit: usize,
}
fn read_limit() -> usize {
    4096
}
#[derive(Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
struct WriteUart {
    id: String,
    run: String,
    channel: u8,
    /// UTF-8 text, or hexadecimal bytes if hex is true. No newline is appended.
    data: String,
    #[serde(default)]
    hex: bool,
}
#[derive(Deserialize, Serialize, JsonSchema)]
#[serde(deny_unknown_fields)]
struct Audio {
    id: String,
    run: String,
    path: PathBuf,
}

fn definition<T: JsonSchema + 'static>(
    name: &'static str,
    description: &'static str,
    read_only: bool,
) -> Tool {
    Tool::new(name, description, serde_json::Map::new())
        .with_input_schema::<T>()
        .with_annotations(
            ToolAnnotations::new()
                .read_only(read_only)
                .destructive(!read_only)
                .open_world(!read_only),
        )
}
fn tools() -> Vec<Tool> {
    vec![
        definition::<Empty>(
            "lisem_catalog",
            "List supported boards, chips and declared buttons/indicators. A board is hardware configuration, an instance owns Flash and OTP.",
            true,
        ),
        definition::<Empty>(
            "lisem_list",
            "List instances with their IDs, paths and UIDs.",
            true,
        ),
        definition::<Instance>(
            "lisem_status",
            "Read instance identity, live state, errors, indicators and UART endpoints. Use runtime.session.output as the run identifier for subsequent controls.",
            true,
        ),
        definition::<Create>(
            "lisem_create",
            "Create an independent instance and UID. Optionally import an original LPK into Flash. Does not power on.",
            false,
        ),
        definition::<Import>(
            "lisem_import",
            "Import LPK ranges into a powered-off instance's Flash, retaining other ranges and OTP.",
            false,
        ),
        definition::<Flash>(
            "lisem_flash_write",
            "Write a binary to Flash at a byte offset. Requires power off; retains OTP.",
            false,
        ),
        definition::<Confirm>(
            "lisem_flash_erase",
            "Erase all Flash to FF, retaining OTP/UID. Requires power off and the current UID.",
            false,
        ),
        definition::<Confirm>(
            "lisem_uid_regenerate",
            "Generate a new persistent UID without erasing Flash. Requires power off and the current UID; firmware cloud binding may need updating.",
            false,
        ),
        definition::<Start>(
            "lisem_power_on",
            "Start original firmware with bounded virtual/host time. Network and host audio are opt-in. No recordings are saved. Firmware may require a held board button to boot.",
            false,
        ),
        definition::<Run>(
            "lisem_power_off",
            "Power off this run, retaining Flash/OTP and enabled UART endpoints.",
            false,
        ),
        definition::<Reset>(
            "lisem_reset",
            "Reset this run; download=true boots the immutable chip ROM in download mode. UART endpoints persist. Obtain the new run identifier afterward.",
            false,
        ),
        definition::<Button>(
            "lisem_button",
            "Press or release a board-declared button. For a hold, call pressed=true, wait, then pressed=false.",
            false,
        ),
        definition::<Uart>(
            "lisem_uart",
            "Enable or disable a host UART endpoint. Can be connected before power-on; POSIX PTY or Windows raw TCP. Enabling persists per instance/channel.",
            false,
        ),
        definition::<ReadUart>(
            "lisem_uart_read",
            "Read bounded in-memory UART history without consuming terminal data. Continue with returned cursor; lost=true means older bytes expired. hex is exact, text is a display decoding. History is limited to 64 KiB per channel and run.",
            true,
        ),
        definition::<WriteUart>(
            "lisem_uart_write",
            "Send raw bytes through the firmware UART FIFO. Include CR explicitly for shell commands. Does not synthesize firmware replies.",
            false,
        ),
        definition::<Instance>(
            "lisem_screenshot",
            "Return the current or last screen as a PNG image, without writing a file.",
            true,
        ),
        definition::<Audio>(
            "lisem_audio_input",
            "Feed a 16 kHz mono PCM16 WAV of at most 60 seconds through ADC. Requires microphone capture off and no queued input.",
            false,
        ),
        definition::<Instance>(
            "lisem_shutdown",
            "Power off and close this instance's runtime and UART endpoints.",
            false,
        ),
    ]
}
fn arguments<T: DeserializeOwned>(value: Value) -> Result<T> {
    Ok(serde_json::from_value(value)?)
}
fn call<T: Serialize>(manager: &mut Manager, method: &str, arguments: T) -> Result<Value> {
    manager.call(method, serde_json::to_value(arguments)?)
}
fn dispatch(manager: &mut Manager, name: &str, value: Value) -> Result<CallToolResult> {
    let result = match name {
        "lisem_catalog" => {
            let _: Empty = arguments(value)?;
            json!({"boards":manager.catalog.boards.values().collect::<Vec<_>>(),"chips":manager.catalog.chips.values().collect::<Vec<_>>()})
        }
        "lisem_list" => {
            let _: Empty = arguments(value)?;
            json!({"instances":manager.catalog.devices()?})
        }
        "lisem_status" => call(manager, "inspect", arguments::<Instance>(value)?)?,
        "lisem_create" => call(manager, "create", arguments::<Create>(value)?)?,
        "lisem_import" => call(manager, "import", arguments::<Import>(value)?)?,
        "lisem_flash_write" => call(manager, "write_flash", arguments::<Flash>(value)?)?,
        "lisem_flash_erase" => call(manager, "erase", arguments::<Confirm>(value)?)?,
        "lisem_uid_regenerate" => call(manager, "regenerate_uid", arguments::<Confirm>(value)?)?,
        "lisem_power_on" => {
            let p: Start = arguments(value)?;
            let options = Options {
                seconds: p.seconds.unwrap_or(300),
                timeout: p.timeout.unwrap_or(800),
                network: p.network,
                host_audio: p.audio || p.microphone,
                microphone: p.microphone,
                sound: !p.mute,
                download: p.download,
                capture: None,
            };
            let state = manager.call("start", json!({"id":p.id,"options":options}))?;
            json!({"instance":p.id,"session":state["sessions"][&p.id]})
        }
        "lisem_power_off" => call(manager, "stop", arguments::<Run>(value)?)?,
        "lisem_reset" => {
            let p: Reset = arguments(value)?;
            call(
                manager,
                if p.download {
                    "reset_download"
                } else {
                    "reset"
                },
                p,
            )?
        }
        "lisem_button" => call(manager, "button", arguments::<Button>(value)?)?,
        "lisem_uart" => call(manager, "serial", arguments::<Uart>(value)?)?,
        "lisem_uart_read" => call(manager, "uart_read", arguments::<ReadUart>(value)?)?,
        "lisem_uart_write" => {
            let p: WriteUart = arguments(value)?;
            manager.call(
                "uart_write",
                json!({"id":p.id,"run":p.run,"channel":p.channel,
                "hex":if p.hex { p.data } else { storage::hex(p.data.as_bytes()) }}),
            )?
        }
        "lisem_audio_input" => call(manager, "audio", arguments::<Audio>(value)?)?,
        "lisem_shutdown" => call(manager, "shutdown", arguments::<Instance>(value)?)?,
        "lisem_screenshot" => {
            let result = call(manager, "screenshot", arguments::<Instance>(value)?)?;
            return Ok(CallToolResult::success(vec![ContentBlock::image(
                result["data"].as_str().context("Missing image")?,
                "image/png",
            )]));
        }
        _ => anyhow::bail!("Unknown tool"),
    };
    Ok(CallToolResult::structured(if result.is_object() {
        result
    } else {
        json!({"result":result})
    }))
}
#[derive(Clone)]
struct Server {
    manager: Arc<Mutex<Manager>>,
    tools: Vec<Tool>,
}
impl ServerHandler for Server {
    fn get_info(&self) -> ServerConfig {
        ServerConfig::new(ServerCapabilities::builder().enable_tools().build())
            .with_server_info(Implementation::new("lisem", env!("CARGO_PKG_VERSION")))
            .with_instructions("Control original firmware in independent instances. Discover board capabilities first. Use run identifiers to reject stale controls, pair button presses with releases, and inspect errors/logs/screens before claiming success. Network and microphone are opt-in. This server stops only runs it started when disconnected. UART history is bounded memory, not an archive.")
    }
    fn get_tool(&self, name: &str) -> Option<Tool> {
        self.tools.iter().find(|t| t.name == name).cloned()
    }
    async fn list_tools(
        &self,
        request: Option<PaginatedRequestParams>,
        _: RequestContext<RoleServer>,
    ) -> std::result::Result<ListToolsResult, ErrorData> {
        if request.and_then(|p| p.cursor).is_some() {
            return Err(ErrorData::invalid_params("Unexpected cursor", None));
        }
        let mut result = ListToolsResult::default();
        result.tools = self.tools.clone();
        Ok(result)
    }
    async fn call_tool(
        &self,
        request: CallToolRequestParams,
        context: RequestContext<RoleServer>,
    ) -> std::result::Result<CallToolResponse, ErrorData> {
        if self.get_tool(&request.name).is_none() {
            return Err(ErrorData::invalid_params("Unknown Lisem tool", None));
        }
        let manager = self.manager.clone();
        let result = tokio::task::spawn_blocking(move || -> Result<CallToolResult> {
            let mut manager = manager
                .lock()
                .map_err(|_| anyhow::anyhow!("Instance manager failed"))?;
            ensure!(
                !context.ct.is_cancelled(),
                "Request cancelled before execution"
            );
            let args = Value::Object(request.arguments.unwrap_or_default());
            ensure!(
                serde_json::to_vec(&args)?.len() <= 65536,
                "Tool arguments exceed 64 KiB"
            );
            dispatch(&mut manager, &request.name, args)
        })
        .await;
        let result = match result {
            Ok(Ok(value)) => value,
            Ok(Err(error)) => CallToolResult::error(vec![ContentBlock::text(format!("{error:#}"))]),
            Err(_) => CallToolResult::error(vec![ContentBlock::text("Lisem tool worker failed")]),
        };
        Ok(result.into())
    }
}
pub fn serve(manager: Manager, stop: Arc<AtomicBool>) -> Result<()> {
    let manager = Arc::new(Mutex::new(manager));
    let runtime = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(2)
        .enable_all()
        .build()?;
    let result = runtime.block_on(async {
        let service = Server { manager:manager.clone(), tools:tools() }.serve(rmcp::transport::stdio()).await?;
        let cancel = service.cancellation_token();
        let mut waiting = Box::pin(service.waiting());
        tokio::select! {
            result = &mut waiting => { result?; }
            _ = async { while !stop.load(Ordering::Acquire) { tokio::time::sleep(Duration::from_millis(50)).await; } } => {
                cancel.cancel(); waiting.await?;
            }
        }
        Ok::<_, anyhow::Error>(())
    });
    runtime.shutdown_timeout(Duration::from_secs(2));
    manager
        .lock()
        .map_err(|_| anyhow::anyhow!("Instance manager failed"))?
        .stop_owned();
    result
}
