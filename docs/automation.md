# 自动化运行

CLI 和 MCP 共用实例、运行标识和状态。自动化测试应使用独立设备库，
运行前核对构建身份；所有输入及运行控制均针对取得的当前运行标识。

## 运行期限

上电时可指定 5～86400 秒的虚拟时长和 1～172800 秒的宿主期限。
虚拟时长控制固件可执行的时间，宿主期限限制本次运行的实际耗时。
两者任一到期都会结束运行；主动下电也可提前结束。

```sh
lisem start DEVICE --seconds 28800 --timeout 36000
lisem status DEVICE
lisem stop DEVICE
```

MCP 使用 `lisem_power_on` 的 `seconds`、`timeout` 参数，
通过 `lisem_status` 观察，通过携带 `run` 的 `lisem_power_off` 结束。
`runtime.session.budget` 给出已配置的期限及最近一次采样的剩余时间。
`runtime_active` 表示运行进程是否存在，`power_hold` 表示板级电源保持
信号是否有效；未驱动或板型未定义该信号时为 `null`。固件的开关机行为
应结合该信号、画面及串口判断。

## 连续接收串口

`uart-receive` 将原始字节写入新文件，接收可跨复位、上下电持续运行：

```sh
lisem uart-receive DEVICE 0 --capture uart0.bin --timeout 36000
```

Ctrl+C 结束接收，设备继续运行。该命令启用对应 UART 端点，接收已有积压
及后续字节；接收期间不要再用其他终端读取同一端点。文件已存在则拒绝覆盖。

MCP 先用 `lisem_uart` 启用端点，再调用 `lisem_uart_capture`：
`action=start` 指定 `path` 和宿主 `timeout`，返回独立的 `capture` 标识；
`action=status` 或 `stop` 携带该标识。状态包含接收字节数及错误。
`lisem_uart_read` 是不消费端点的旁路观察，历史溢出时报告 `lost`，
不能代替持续接收。此前运行的积压按原顺序保留，可通过接收归档排空。

## WAV 注入与音频证据

输入为 16000 Hz、单声道 PCM16 WAV，最长 24 小时。文件在注入期间应保持
可读且内容不变。CLI 按 1:1 宿主节拍等待固件 ADC 消费完成；Ctrl+C 取消
剩余输入。固件未读取 ADC 时不会报告注入完成，实际运行须有足够的期限。

```sh
lisem audio-input DEVICE speech.wav
lisem audio-output DEVICE --capture speaker.wav
```

音频输出命令持续侧录原始 DAC 样本，Ctrl+C 结束侧录。侧录独立于宿主播放，
不会因扬声器静音而丢失 DAC 数据；缓冲溢出、采样率变化和运行中断明确报错。
MCP 的 `lisem_audio_input` 返回注入标识；通过状态中的 `audio_input` 观察，
用 `lisem_audio_input_cancel` 携带运行及注入标识取消。

`audio_output` 状态包含样本数、非零样本数、峰值、最后样本及最后非零样本
的虚拟时刻，以及 PA 状态。这些数据用于定位播放活动；语音内容和音质须
分析录制文件，不能仅凭计数判定。长录音宜按用例分段，保留输入及输出证据。

## 预置动作

`lisem events DEVICE plan.json --run RUN` 在当前运行中布置一组动作，返回
程序标识。`lisem events-status DEVICE SEQUENCE --run RUN` 查询状态，
加 `--cancel` 可取消待执行动作及该程序仍持有的按键或音频输入。
MCP 对应 `lisem_events` 的 `arm`、`status`、`cancel`。

以下程序等待 UART 0 后续出现 `ready` 的原始字节，随后延迟 250 ms 按键：

```json
{
  "trigger": {"uart_hex": "7265616479", "channel": 0, "timeout_ms": 10000},
  "actions": [
    {"after_ms": 250, "kind": "button", "button": "function",
     "count": 1, "hold_ms": 80, "gap_ms": 0}
  ]
}
```

也可用 `{"at_ns": 10000000000}` 在本轮运行的第 10 个虚拟秒触发。
`after_ms` 相对于触发时刻，以虚拟时间执行；UART 匹配不回看历史，不消耗
终端数据。模式为 1～128 字节，等待期限为 1～86400000 虚拟毫秒。

动作可为 `button`、`audio`（WAV 的绝对 `path`）、`access_point`
（`enabled`）或 `power_off`。每组 1～32 个动作按 `after_ms` 排序，
下电动作必须在最后；触发期限及动作必须在剩余运行预算内。
`access_point` 只改变本轮热点可用状态。

`dispatched` 表示动作已经交付。按键序列和 WAV 可能仍在执行，应继续观察
返回的 `button_sequence`、`audio_sequence` 标识；业务成功须由固件输出
确认。复位和手动按键取消待执行动作。WAV 注入可用来触发已经布置的
UART 条件；若程序还包含音频动作，须保证动作执行时 ADC 输入未被占用。
超时、运行中断和动作失败各自有明确状态，不能当作完成。
