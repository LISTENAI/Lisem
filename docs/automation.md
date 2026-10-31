# 命令行自动化

使用独立设备库执行命令行自动化，运行前核对构建身份；所有输入及运行控制
均针对取得的当前运行标识。基础用法与 BLE 操作见[使用说明](usage.md)，
MCP 工具调用见[agent 接入](agents.md)。

普通脚本和 pytest 可使用随应用提供的 [Python SDK](../python/README.md)，
通过上下文创建临时实例、等待输入确认和保存失败证据。
固定 UID 与 LPK 的试跑入口见 [固件测试示例](../python/examples/README.md)。

## 运行期限

上电时可指定 5～86400 秒的虚拟时长和 1～172800 秒的宿主期限。
虚拟时长控制固件可执行的时间，宿主期限限制本次运行的实际耗时。
两者任一到期都会结束运行；主动下电也可提前结束。
桌面上电使用 24 小时虚拟时间、48 小时宿主时间的上限。

```sh
lisem start DEVICE --seconds 28800 --timeout 36000
lisem status DEVICE
lisem stop DEVICE
```

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

`audio_output` 状态包含样本数、非零样本数、峰值、最后样本及最后非零样本
的虚拟时刻，以及 PA 状态。这些数据用于定位播放活动；语音内容和音质须
分析录制文件，不能仅凭计数判定。长录音宜按用例分段，保留输入及输出证据。

## 摄像头输入

使用图片作为传感器场景，或查询并选择宿主摄像头：

```sh
lisem camera DEVICE image.png
lisem camera-devices
lisem camera DEVICE --device DEVICE_ID
lisem camera DEVICE --clear
```

`DEVICE_ID` 使用设备查询返回的标识。

图片支持 PNG、JPEG、PNM，至多 32 MiB，宽高各不超过 4096 像素。图片与
实时画面均按板型安装方向转换，等比居中裁剪至 640×480。
Linux 摄像头需为可访问的 V4L2 单平面流式设备，支持 YUYV、UYVY、RGB24、
BGR24 格式。

## 长按与连按

自动化按键使用虚拟时间序列，一次提交完整的按下与松开动作：

```sh
lisem button-sequence DEVICE function --run RUN --count 3 --hold-ms 80 --gap-ms 80
lisem button-sequence-status DEVICE SEQUENCE --run RUN
lisem button-sequence-status DEVICE SEQUENCE --run RUN --cancel
```

`RUN` 取自 `lisem --json status DEVICE` 的 `runtime.session.output`，
`SEQUENCE` 使用提交返回的标识。次数为 1～32，总时长至多 60 秒，须在剩余
运行预算内。手动按键接管时取消序列；复位、下电会终止序列并释放按键。
`completed` 表示输入完成，固件是否识别需观察实际输出。

## 数字信号时序

先查询可用源，再携带当前运行标识开始采集：

```sh
lisem signal-trace DEVICE
lisem signal-trace DEVICE --run RUN --start SOURCE --capacity 1024
lisem signal-trace DEVICE --run RUN --trace CAPTURE --after 0 --limit 128
lisem signal-trace DEVICE --run RUN --trace CAPTURE --stop
```

`RUN` 同上，`SOURCE` 使用查询返回的源 ID 或网络名，`CAPTURE` 使用开始
采集返回的标识。事件包含序号、虚拟纳秒时间、逻辑电平和 `driven` 驱动状态，
`initial=true` 表示初始状态。

读取后用 `next_cursor` 作为下一次的 `after`，直到 `has_more=false`。
最多保留 4096 条事件，`evicted_events` 为累计淘汰数，`lost_events` 为当前
游标漏掉的条数。复位或下电结束采集，记录保留到下一次采集或运行。
`unknown` 须继续查询，`interrupted` 表示无法确认完整结束记录。
采集默认关闭，仅保存在有界内存中，需要时显式保存命令输出。

事件反映器件模型的数字变化；未驱动时的逻辑电平不代表实际模拟电压，
物理引脚波形及模型未生成边沿的总线时序仍需真机测量。

## 预置动作

`lisem events DEVICE plan.json --run RUN` 在当前运行中布置一组动作，返回
程序标识。`lisem events-status DEVICE SEQUENCE --run RUN` 查询状态，
加 `--cancel` 可取消待执行动作及该程序仍持有的按键或音频输入。

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

## SD 卡镜像

挂载可写 raw 镜像，可在运行中插入和弹出：

```sh
lisem sd-card DEVICE --image card.img
lisem sd-card DEVICE
lisem sd-card DEVICE --eject
```

镜像大小须为 8 MiB～32 GiB 的二次幂。先使用宿主工具准备固件支持的分区、
文件系统和测试文件，再卸载宿主文件系统并交给模拟器。零填充文件只代表
未格式化介质。卡识别、文件系统挂载、文件读取及播放分别由固件完成。

挂载路径按实例保存，上电时重新打开原文件。运行中变更在当前运行确认后
保存；也可用 `--run` 明确指定当前运行。若返回 `pending`，
查询状态中的 `sd_change` 直到确认或报错。固件直接写回镜像，同一镜像不能
由多个实例或宿主工具同时写入。弹出会让固件看到卡移除，未完成的文件系统
写入可能使镜像损坏；需要可重复试验时使用镜像副本。

## 信号输入

`lisem signals` 枚举板型提供的输入，包括类型、单位、范围、
默认值和当前值。数字量使用布尔值表示高低电平，电压使用整数毫伏：

```sh
lisem signals DEVICE
lisem signals DEVICE --set '{"usb_det":false,"battery_voltage":3800}'
lisem signals DEVICE --set '{"usb_det":null}'
```

按输入 ID 更新，`null` 恢复该输入的板型默认值。设置按实例保存，可在运行中
修改。命令自动取得当前运行标识，也可显式传入 `--run`。返回 `pending` 时，
继续查询并核对 `change.request`，直到 `applied`、`rejected` 或 `unknown`；
后两者及 `error` 均须检查，不能当作完成。

Arcs-Mini 的 `usb_det` 连接 PB0；高低电平含义由固件
解释。`battery_voltage` 经 2/5 分压接入 PB5/VIN3，固件从 ADC 读取电压。
输入已应用表示外部信号已送达，固件的滤波、轮询和业务响应需要另外观察。
