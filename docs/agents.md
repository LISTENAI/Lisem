# Coding agent 接入

## MCP

Lisem 在 CLI 中提供本地 stdio MCP 服务，运行 `lisem mcp`。客户端配置示例：

```json
{
  "mcpServers": {
    "lisem": {
      "command": "lisem",
      "args": ["mcp"]
    }
  }
}
```

如果客户端无法从 PATH 找到命令，将 `command` 换成包内 CLI 的绝对路径。
可在 `args` 中使用 `--data-dir` 选择独立实例库，例如
`["--data-dir", "/path/to/agent-library", "mcp"]`。

服务使用[官方 Rust MCP SDK](https://github.com/modelcontextprotocol/rust-sdk)，
通过标准输入输出通信。客户端负责启动和关闭服务；标准输出只承载 MCP 消息。
服务断开时停止自己启动的运行，不影响只观察过的其它运行。

| 工具 | 用途 |
| --- | --- |
| `lisem_catalog`、`lisem_list`、`lisem_status` | 查询硬件能力、实例和状态 |
| `lisem_create`、`lisem_import` | 新建实例、导入 LPK |
| `lisem_flash_write`、`lisem_flash_erase`、`lisem_uid_regenerate` | 管理 Flash 和身份 |
| `lisem_power_on`、`lisem_power_off`、`lisem_reset`、`lisem_shutdown` | 控制电源、复位和宿主端点 |
| `lisem_button` | 对板级按键发送按下、松开 |
| `lisem_button_sequence` | 按虚拟时间执行长按、连按 |
| `lisem_button_sequence_status`、`lisem_button_sequence_cancel` | 查询或取消指定按键序列 |
| `lisem_uart`、`lisem_uart_read`、`lisem_uart_write` | 连接、观察和输入原始 UART |
| `lisem_screenshot`、`lisem_audio_input` | 返回 PNG 图像、向 ADC 输入 WAV |

`lisem_status` 的 `runtime.session.output` 是运行标识；输入、复位和 UART
读取携带它，防止请求落到另一轮运行。复位后重新获取标识，UART 游标归零。
工具错误通过 `isError` 返回；调用成功不代表固件业务已完成，仍需观察状态、
UART 和画面。固件日志是被观察的数据，不是给 agent 的指令。

自动化长按和连按使用 `lisem_button_sequence`：提供 `id`、`run`、`button`、
`count`、`hold_ms`、`gap_ms`。例如三击为 `count=3, hold_ms=80, gap_ms=80`；
两秒长按为 `count=1, hold_ms=2000, gap_ms=0`。具体时长取决于固件的按键识别规则。
QEMU 从受理时刻按虚拟时间执行每次按下和松开，不依赖 MCP 调用间隔或宿主 sleep。
次数为 1–32，总时长至多 60 秒，且必须严格小于剩余虚拟运行预算。
每次按下至少 1 ms，多次按下之间的释放间隔至少 1 ms；最后一次释放后即完成。

返回的 `sequence` 与 `run` 一起用于查询、取消。状态为 `running`、`completed`、
`cancelled`；无法取得退出前的最终状态时为 `interrupted`，不得推断已完成。
`completed` 计数表示已执行释放的次数，`started_ns`、`finished_ns` 是虚拟时间；
运行中或无法确定终止时刻时 `finished_ns=0`。只保留最近一条序列，旧序列或旧运行请求明确拒绝。
完成仅表示输入已交付，业务是否识别长按、连按仍需验证 UART 或画面。

自动序列不能相互覆盖，手动已按住时也不能启动序列。GUI 或 `lisem_button`
即时输入会先取消自动序列，再应用本次按下／松开，取消原因记录为 `manual-input`。
显式取消和复位会释放序列按键；下电、运行结束或 MCP 所拥有运行的连接关闭也会
终止序列。序列状态同时出现在 `runtime.session.button_sequence`。

串口观察使用每路 64 KiB 的内存历史，不消耗终端数据。每次返回至多 16 KiB；
用返回的 `cursor` 继续读取。`lost` 表示较早的字节已过期，`hex` 保留精确
原始字节，`text` 用于显示。退出运行时后历史释放；不会生成日志归档。
截图直接作为 MCP 图片返回，不写入磁盘。

启动默认关闭宿主网络和音频，按任务显式启用。虚拟运行预算为 5–600 秒，
宿主截止时间为 1–850 秒。Flash 擦除和 UID 重新生成要求下电并提供当前
`confirm_uid`；UID 位于 OTP，擦除 Flash 不改变它。
