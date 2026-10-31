# 固件测试示例

通用预置动作可先用内置 ROM 运行 [events.py](events.py)：
`lisem python -- events.py --output ./events-result`。输出使用新的空目录；该例确认
按键子输入完成，接口与计划结构见 [预置动作任务](../docs/inputs.md#预置动作任务)。

示例位于应用的 `runtime/python-docs/examples/`，也在源码的 `python/examples/`。
API 参考见 [Python API](../docs/README.md)。在所选测试虚拟环境安装 pytest、
Hypothesis，并在本目录通过 Lisem 启动：

```sh
export LISEM_TEST_LPK=/path/to/arcs-mini-v3.0.3.lpk
lisem python -- smoke.py --lpk "$LISEM_TEST_LPK" --output ./script-result
lisem python -- -m pytest test_firmware.py -q --basetemp ./pytest-result --junitxml ./result.xml
lisem python -- -m pytest test_peripherals_firmware.py -q --basetemp ./media-result
```

输出使用新的空目录。`LISEM_TEST_BOARD=arcs-mini3` 配合对应的 3.0.3 包
可复用 pytest 示例；`LISEM_TEST_UID` 可指定预先分配的 prefixed UID。
本例采用离线固件环境。其他固件版本应先核对启动流程与业务标记。

`firmware.py` 定义 ARCS 3.0.3 的 OTP 校验、开机步骤、UID 命令和单击进入
信息页的业务断言。WAV 用例检查真实 ADC 样本消费；唤醒或语音识别用例需
另行准备匹配素材和业务判据。

`test_peripherals_firmware.py` 需要宿主安装 `adb`，验证真实 shell 的版本和
UID 响应、断开重连、SD 介质变更及固件 DAC 录制。录制同时保存原始
时间戳和 PA 状态；用例使用独立的空白 SD 镜像。

显示观测样例通过固件已有命令调节亮度，并保存空闲动画的真实截图序列：

```sh
lisem python -- -m pytest test_display_firmware.py -k brightness -q --basetemp ./display-result
LISEM_TEST_LONG_POWER=1 lisem python -- -m pytest test_display_firmware.py -k hibernate -q \
  --basetemp ./display-power-result
```

后一个用例按固件五分钟期限等待熄屏，再通过 USB 检测输入唤醒。
显示占空比与截图各自保留虚拟时刻；样例的固件亮度映射不进入 SDK。

热点示例通过固件已有的 `wifi connect`、`wifi disconnect` 命令发起真实
连接，观察 AP 的关联状态和撤下、恢复及清空列表后的即时状态：

```sh
lisem python -- -m pytest test_wifi_presence_firmware.py -q --basetemp ./wifi-presence-result
```

该例使用离线热点，不检查云端可用性；固件的断连事件和自动重连策略由
业务测试另行判定。

上电前输入示例分别在 USB 检测为真、假时启动固件，核对板型输入与固件
实际观察的外部供电状态：

```sh
lisem python -- -m pytest test_boot_inputs_firmware.py -q --basetemp ./boot-inputs-result
```

## BLE 属性探索

[ble.py](ble.py) 提供 `inspect_peripheral(run)`，在已启动的 Arcs 固件运行中
扫描广播、建立连接、发现服务和特征，并读取声明可读的属性。调用前由测试
工程执行固件需要的开机与广播入口；函数返回属性元数据及 bytes 值，连接
和中心端随上下文退出清理。

写入、CCCD 订阅及通知观察见 [BLE API](../docs/ble.md)。应用协议的报文构造
和业务结果断言放在调用工程中。

## 状态机与重放

状态机在每次尝试中重建固定 UID、指定 LPK 的临时实例，检查 UID 响应、
外部数字输入与按键边沿，并保存动作步骤、构建身份和观测证据：

```sh
lisem python -- stateful.py --lpk "$LISEM_TEST_LPK" --output ./stateful-result
```

故障演示刻意设置“禁止连按”的测试判据，用于观察失败留存、缩减与重放。
触发该判据时进程退出码为 1；结果和较小复现步骤保存在输出目录。

```sh
lisem python -- stateful.py --lpk "$LISEM_TEST_LPK" --output ./demo-result --demo-failure
lisem python -- stateful.py --lpk "$LISEM_TEST_LPK" --output ./replay-result \
  --replay ./demo-result/minimal-replay.json --demo-failure
```

重放前核对记录中的固件摘要和判据，并恢复相同的初始环境。本例采用固定
本地输入；含网络、云端或实时外设的测试还需恢复相应外部条件。
