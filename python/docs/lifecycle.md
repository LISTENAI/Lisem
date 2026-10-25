# 客户端、实例与运行

[API 总览](README.md) · [错误与清理](errors.md)

## Client

```python
Client(*, executable=None, data_dir=None, root=None)
```

随包 SDK 默认选择同一应用内的 `lisem`。显式 `executable` 或
`LISEM_EXECUTABLE` 须指向该配套程序，否则在创建实例前报告兼容错误。
`data_dir` 默认为 `~/.cache/lisem-python/library`，测试宜使用独立库；随包
SDK 的 `root` 保持 `None`，资源由应用确定。构造时核对实际原生握手，worker
构建须与管理程序一致，匹配信息保存在 `client.sdk` 与 `client.build` 中。

| 调用或属性 | 返回及作用 |
| --- | --- |
| `client.build` | 构建字典：`version`、`source_sha256`、`python_source_sha256`、`target` |
| `client.catalog()` | `dict`，芯片、板型、能力清单 |
| `client.instances()` | `list[dict]`，设备库实例状态 |
| `client.camera_devices()` | 原生摄像头枚举字典，供选择设备标识 |
| `client.instance(identifier)` | 连接既有实例，返回 `Instance`，复用本客户端未关闭句柄 |
| `client.mark_failed(reason)` | 标记测试失败，返回 `None` |
| `client.recover()` | 回收结果列表，见[回收](errors.md#异常退出后的回收) |
| `client.close()` | 清理自有上下文与管理会话，正常返回 `None`，可重复调用 |

使用 `with Client(...) as client` 管理会话。连接既有实例时保留其 Flash、OTP
和配置；其他调用者的运行由其所有者管理。

## 临时实例

```python
client.temporary_instance(
    *, board, uid=None, lpk=None, name=None,
    allow_unprefixed_uid=False, evidence_dir=None,
)
```

返回上下文管理器，进入时创建并连接实例，得到 `Instance`。`board` 为板型
ID；`lpk` 为一次性导入的应用固件路径，省略时使用新存储和内置 ROM；`name`
为可选名称。`uid` 省略时生成 prefixed UID，显式 UID 按芯片校验；使用非预设
前缀时，本次独立传入 `allow_unprefixed_uid=True`。UID 的显示及 OTP 转换按
具体芯片处理，同库重复身份拒绝；并行任务协调 UID 与可写介质分配。

`evidence_dir` 须为空且位于设备库 `devices/` 之外。退出时结束自有运行，保存
显式请求的证据，确认 worker 与存储租约退出后清理临时设备。所有权不确定时
保留现场，处理方法见[错误与清理](errors.md)。

## Instance

主要属性为 `id: str`、`uid: str`、`path: Path`、`info: dict`、`current_run`
（最近启动的 Run，启动前为 `None`）。`info` 为最近设备描述快照，含硬件及
`capabilities`；即时状态使用查询方法。

| 调用 | 返回及作用 |
| --- | --- |
| `device.status()` | worker 状态字典，含 `worker`、`session`；下电时 session 可为空 |
| `device.inputs(*, timeout=10)` | 声明输入与当前值，见[板型输入](inputs.md#板型输入) |
| `device.set_inputs(changes, *, timeout=10)` | 上电前保存输入，返回 `Delivery` |
| `device.save_evidence(directory, *, failure=None)` | 保存设备及各轮证据，返回 `None` |
| `device.close()` | 关闭本句柄自有运行及 worker，保留既有实例存储，返回 `None` |

`device.wifi`、`device.sd`、`device.camera` 用于上电前配置；运行中使用
相应的 Run 接口，见[输入与外设](inputs.md)。

## 启动与 Run

```python
device.start(
    *, max_virtual_seconds=120, timeout=180,
    network=False, host_audio=False, download=False,
)
```

返回支持上下文管理的 `Run`。`max_virtual_seconds` 为 5～86400 的整数虚拟
秒，`timeout` 为 1～172800 的整数宿主秒，任一预算到期结束整轮运行。
`network=True` 启用宿主网络桥接；`host_audio=True` 显式选择宿主音频接入。
启动始终关闭麦克风和扬声器播放，文件注入和 DAC 侧录可独立使用。
`download=True` 让支持的板型进入 ROM 烧录模式。

`run.id` 为实际运行标识，`run.instance` 为所属实例；操作均校验当前轮次。

| 调用 | 返回及语义 |
| --- | --- |
| `run.status()` | session 字典，含 `finished`、`seconds`、`budget`、`error`、`qemu`、`display` 及输入状态 |
| `run.wait(predicate, *, timeout=10, description=None)` | 将状态交给谓词，返回首次满足的状态；见[观察期限](errors.md#观察期限) |
| `run.reset(*, download=False)` | 保留存储，停止并重启后端，返回新的 Run；后续使用返回句柄 |
| `run.save_evidence(directory)` | 保存本轮操作、观察与可用截图，返回 `None` |
| `run.close()` | 取消自有任务，采样并确认本轮停止，正常返回 `None` |

`qemu` 保留实际版本和 SHA-256，独立于 `client.build`。复位后旧 Run 退休，
继续操作会报告 `StaleRun`。Run 上下文退出结束本轮；停止未知时报告清理错误
并保留现场。此复位为后端重启，物理电源故障需另行建模和验证。

## 启动器、IDE 与 Notebook

`lisem python [--interpreter PATH] -- <Python 参数>` 原样传递脚本、`-m`、`-c`
和帮助参数。默认沿用活动 venv/Conda，否则在 PATH 查找（POSIX 先 python3，
Windows 先 python）。活动环境缺少解释器时明确报错，可显式选择其他环境。
启动器只调整本次进程的 SDK 搜索路径与配套 CLI，正常原生
CLI、GUI 和 QEMU 运行独立于 Python。

`lisem python --sdk-path` 输出 SDK zip 路径，`lisem --json python --sdk-path`
还给出版本、实际编译身份、CLI 路径和 SDK 清单哈希。`lisem build-info` 仅查询
原生编译身份；这些查询不打开设备库。既有独立安装 SDK 或当前目录同名模块
不能覆盖启动器选择的 SDK；冲突的启动配置会明确报错。

Notebook 在首次导入前设置路径与字节码选项：

```python
import subprocess
import sys

sdk_path = subprocess.check_output(["lisem", "python", "--sdk-path"], text=True).strip()
sys.path.insert(0, sdk_path)
from lisem import Client, source_info
print(source_info())
```

已导入其他 lisem 模块的内核先重新启动。已有进程也可将查询返回的 zip 加入 PYTHONPATH；SDK 与标准包元数据通过
Python zipimport 读取，pytest 可发现插件，普通导入不会在应用内生成字节码缓存。`-E`、`-I`、`-S` 禁用
环境或 site 初始化，启动器会拒绝；需要这些模式时由外部解释器自行配置路径。

POSIX 直接 exec Python，保持 PID 和信号语义；Windows 等待同控制台 Python，
保留其退出状态，Ctrl+C 交给 Python；异常退出时约束直接 Python，后台子进程
保留原有 CLI 所有权和生命周期。
其他调用者已运行的实例由其所有者管理。
