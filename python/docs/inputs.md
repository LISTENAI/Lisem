# 输入与外设

[API 总览](README.md) · [观测与采集](observations.md) · [错误与清理](errors.md)

Wi-Fi、SD 和摄像头在上电前通过 `device` 保存，运行中通过当前 `run` 修改。
设置等待保存或应用确认，返回 `Delivery`；固件负责输入的业务解释。

## 按键

`run.button(button="function", *, count=1, hold_ms=80, gap_ms=80, timeout=10)`
执行板型按键 ID 的序列，返回状态 `completed` 或已确认释放的 `cancelled`。
`count` 为 1～32；`hold_ms` 为 1～60000 虚拟毫秒；`gap_ms` 为 0～60000，
连按时须大于 0；总序列最长 60000 虚拟毫秒。宿主等待超时后取消剩余输入，
确认释放；按键导致的固件复位结合取消原因与启动输出判断。

## 板型输入

| 签名 | 返回 |
| --- | --- |
| `device.inputs(*, timeout=10)` | 字典：`inputs` 列表、`change`、`run` |
| `device.set_inputs(changes, *, timeout=10)` | 上电前保存，状态 `saved` |
| `run.inputs()` | 当前运行的同形输入字典 |
| `run.set_inputs(changes, *, timeout=10)` | 当前运行应用，状态 `applied` |

输入行含 ID、类型、默认值、`value`、`overridden`；数字量为 bool，电压为
整数 mV。名称与范围由板型声明。`changes` 是 ID 到值的字典，部分更新保留
其他项，`None` 恢复该项默认值。非法或未知字段整体拒绝；活动运行期间使用
Run 写入。无声明输入的板型返回空列表。

## Wi-Fi

以下签名同时用于 `device.wifi`、`run.wifi`：

| 签名 | 返回 |
| --- | --- |
| `configure(access_points, *, timeout=10)` | `Delivery`，替换并保存整个热点环境 |
| `set_present(enabled, *, timeout=10)` | `Delivery`，保存或应用 bool 总可用性 |
| `configuration()` | `dict`，`access_points` 为保存的列表，含密码 |
| `status(*, timeout=10)` | `dict`，含 `configured_present` 和 `runtime` |

最多 16 个热点，每项提供 `ssid`（1～32 UTF-8 字节，无 NUL）、`rssi`
（-127～0 dBm 整数）、`channel`（1～14）、`internet`（bool）。开放热点
省略安全字段；WPA2 使用 `security="wpa2-psk"`，`password` 为 8～63 个
可打印 ASCII 字符。

下电时 `runtime=None`；运行时含 `instance_id`、`run`、`present`、
`sampled_virtual_ns`、`access_points`，无密码。AP 的 `associated`、
`authorized` 来自真实关联与握手；缺少即时观测能力抛出 `CompatibilityError`。

`configure([])` 保存空列表，保留总开关；`set_present(False)` 撤下热点并
取消关联、保留配置，再启用需固件重新关联。此开关按实例保存，芯片 MAC 由
固件控制。单 AP `internet=False` 禁止外网双向转发，保留关联及本地 ARP、
DHCP；外网转发还需 `start(network=True)`。配置查询中的密码按凭据保管。

## SD 与摄像头

以下分别用于 `device.sd` / `run.sd` 和 `device.camera` / `run.camera`：

| 签名 | 返回与条件 |
| --- | --- |
| `sd.mount(path, *, timeout=10)` | `Delivery`；可写 raw 镜像，8 MiB～32 GiB 的 2 次幂，独占写锁 |
| `sd.eject(*, timeout=10)` | `Delivery`，移出介质 |
| `sd.status()` | 介质字典或 `None` |
| `camera.set_image(path, *, timeout=10)` | `Delivery`，PNG、JPEG 或 PNM 场景 |
| `camera.use_device(device_id, *, timeout=10)` | `Delivery`，显式选择枚举所得非空设备标识 |
| `camera.clear(*, timeout=10)` | `Delivery`，清除来源 |
| `camera.status()` | `camera_image`、`camera_device`；Run 查询还含 `camera_change`、`camera_capture` |

并行设备使用独立 SD 镜像，文件系统由固件操作。摄像头运行中选择等待首帧及
应用确认，权限和断开错误须处理；Linux 使用未压缩单平面 V4L2。场景经传感器
与 DMA 到达固件，采集成功按实际输出判断。

## USB ADB

`run.usb_adb.enable(*, timeout=10)`、`disable(*, timeout=10)` 返回本轮状态
字典并确认 `enabled`；`status()` 返回含 `run`、`enabled`、`connected`、
`status`、`error` 的状态。开启后的 `listen` 是回环地址，供自行安装的 adb
连接。监听、传输及固件 USB 状态分别观察；shell 由固件执行。

## 预置动作任务

`task = run.events(program)` 返回绑定当前运行的任务；`task.sequence` 为标识，
`task.state` 为最近程序状态。`program` 含 `trigger` 和 `actions`：触发使用
本轮虚拟 `at_ns`，或 `uart_hex`、`channel`、`timeout_ms` 等待后续 UART。
动作使用相对触发的 `after_ms`，kind 为 `button`、`audio`、`access_point`
或 `power_off`。以下在当前虚拟时刻后一秒按键：

```python
program = {
    "trigger": {"at_ns": int((run.status()["seconds"] + 1) * 1e9)},
    "actions": [{"after_ms": 0, "kind": "button", "button": "function",
                 "count": 1, "hold_ms": 80, "gap_ms": 0}],
}
task = run.events(program)
result = task.wait_inputs(timeout=5)
```

完整 ROM 例子见 [events.py](../examples/events.py)。UART 触发使用
`{"uart_hex": "7265616479", "channel": 0, "timeout_ms": 10000}`，匹配触发后
新收到的字节；模式 1～128 字节，期限 1～86400000 虚拟毫秒。动作 1～32 项
按 `after_ms` 排序，必须在剩余运行预算内；button 字段同按键接口，audio 提供
WAV 绝对 `path`，access_point 提供 bool `enabled`，power_off 排在最后。

| 任务调用 | 返回与作用 |
| --- | --- |
| `task.wait(*, timeout=10)` | `Delivery("dispatched", state)`，子输入可能仍在执行 |
| `task.wait_inputs(*, timeout=10)` | 确认按键/WAV 子输入完成，返回 `Delivery("completed", {"program": ..., "children": ...})` |
| `task.cancel()` | 取消程序自有动作与输入并确认，返回 `None` |

`children` 可含 `button`、`audio` 最终状态。超时尝试取消，确认未知时报告
`DeliveryUnknown`。复位、下电及 Run 关闭取消旧输入。预置热点动作只影响
本轮可用性，持久化使用 Wi-Fi 接口。WAV 注入与录制见[观测与采集](observations.md)。
