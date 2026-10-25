# 观测与采集

[API 总览](README.md) · [期限与异常](errors.md)

观察句柄绑定当前 Run。完整历史断言在触发输入前建立观察；复位后重新创建。

## UART

`uart = run.uart[channel]` 选择板型声明的 UART 通道。

| 签名或属性 | 返回与含义 |
| --- | --- |
| `uart.write(data)` | 接受 `bytes`，返回 `Delivery("queued", {"bytes": ...})` |
| `uart.watch(*, cursor=None, max_bytes=1048576)` | 返回支持上下文管理的 watch |
| `watch.read(*, timeout=5)` | `bytes`，最多 16384 字节并推进游标，暂无数据可为空 |
| `watch.expect(pattern, *, timeout=10, regex=False)` | `bytes`，未匹配缓冲开头至匹配末尾 |
| `watch.cursor`、`start_cursor` | 当前与初始字节游标 |
| `watch.observed`、`lost` | 累计原始字节 `bytearray`，以及丢失标志 |

`cursor=None` 从创建观察时的最新字节之后开始，`0` 请求本轮仍保留的历史；
显式游标为非负整数。`max_bytes` 范围 1～16777216，默认累计上限 1 MiB。
后端历史淘汰或句柄溢出抛出 `HistoryLost`。

`pattern` 为非空 `bytes`，`regex=True` 使用字节正则。`read()` 的字节进入
匹配缓冲；`expect()` 消费至匹配末尾，保留后续缓冲及累计 `observed`。
旁路观察不消费终端数据，watch 上下文退出不关闭 UART。

## 截图与显示

`run.screenshot()` 返回 `Screenshot`，含 `png: bytes`、`metadata: dict`。
`image.save(path)` 写 PNG 及 `path + ".json"` 元数据，创建父目录并返回
`Path`，同名输出会覆盖。像素时刻取自 `metadata["frame"]["virtual_ns"]`，
同帧序号与运行身份一起保留。

`run.display_status()` 返回即时字典：`available`、`instance_id`、`run`、
`sampled_virtual_ns`、`displays`。显示项按板型器件 ID 索引，无器件时为空。
每项包含 `controller_id`、`enabled` 及 `backlight`：

| backlight 字段 | 含义 |
| --- | --- |
| `device_id`、`unit` | 板型背光器件 ID，单位 `ratio` |
| `drive_duty` | 送到面板模型的驱动占空比，0～1 |
| `effective_duty` | 显示使能门控后占空比，关闭时 0 |

驱动值与显示有效值分别保留，固件亮度百分比映射属于业务测试。状态采样与
像素时刻独立；`run.status()["display"]` 为最近采样，需要即时值时调用上述
接口。查询不主动推进虚拟时间或生成 PWM 边沿，缺能力时抛出 `CompatibilityError`。

## 数字信号

`trace = run.signal_trace(sources, *, capacity=1024)` 返回采集句柄，支持上下文
管理。`sources` 选择 1～96 个板型数字源 ID 或 net 名称，后端容量为 1～4096
且须容纳所有选中源的初始状态。初始与后续驱动变化均以虚拟时刻记录。

| 成员 | 返回与含义 |
| --- | --- |
| `trace.read()` | 下一页字典，最多 512 事件，推进游标 |
| `trace.stop()` | 确认停止并排空，首次正常返回累计事件列表，已结束时返回 `None` |
| `trace.cancel()` | 确认停止，返回 `None`，取消不承诺排空 |
| `trace.capture`、`cursor` | 采集标识与已读序号 |
| `trace.pages`、`events` | 累计页面和事件列表 |

页含 `capture`、`run`、`status`、`sources`、`next_cursor`、`has_more`、
`evicted_events`、`lost_events`、`events`，以及起止 ns。事件保留序号、
`virtual_ns`、源、net、驱动状态与电平。SDK 累计上限 16384 事件，历史缺口
抛出 `HistoryLost`，中断抛出 `RunEnded`。上下文退出调用 `stop()`。

## WAV 输入

`run.audio.inject(path, *, timeout=15)` 同步等待 ADC 消费，返回
`Delivery("completed", raw)`。文件为 16000 Hz、单声道 PCM16 WAV，最长
24 小时，注入期间保持可读且内容不变。输入按 ADC 虚拟采样及 1:1 宿主节拍
执行。宿主等待超时后取消剩余输入，确认后报告 `OperationTimeout`，取消
未知则为 `DeliveryUnknown`；固件未消费或运行中断不会报告完成。

## DAC 录制

```python
with run.audio.record("result/response.wav", max_seconds=30) as recording:
    run.button()
    # 在这里等待所需的实际固件响应。
print(recording.metadata)
```

`run.audio.record(path, *, max_seconds=30)` 创建并返回后台录制句柄；
`max_seconds` 为大于 0 且不超过 600 的宿主安全期限，到期结束录制并报告
`OperationTimeout`。录制独立于扬声器播放。

| 成员 | 返回与含义 |
| --- | --- |
| `recording.stop(*, timeout=12)` | 确认停止及排空，成功返回 `Delivery("completed", metadata)`；timeout 为大于 0 且不超过 60 的宿主秒数 |
| `recording.cancel()` | 按默认停止期限结束录制，返回交付结果，也用于 Run 清理 |
| `recording.metadata`、`frames` | 状态、Run、设备 ID、sequence、起点、路径及帧格式；已收样本帧数 |
| `recording.path`、`raw_path`、`metadata_path` | 三份输出的 Path |

输出须位于实例存储之外，WAV、`path + ".frames"`、`path + ".json"` 均须
尚不存在。WAV 按虚拟时刻放置原始 DAC PCM16，缺样本间隔写入静音。原始帧
保留小端 `ns:u64, rate:u32, sample:i16, pa:u8, reserved:u8`，每帧 16 字节，
上限 1 GiB。丢帧、采样率变化、时刻倒退或运行中断使录制失败，文件和错误
保留。停止未知报告 `DeliveryUnknown`；复位先结束本轮录制。录音结束保留设备运行。
