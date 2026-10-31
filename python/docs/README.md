# Python API

SDK 为同步接口；同一实例的操作顺序执行，多实例使用独立上下文或进程。
`device` 表示 `Instance`（Flash、OTP 与配置），`run` 表示一次实际启动。

| 文档 | 内容 |
| --- | --- |
| [生命周期](lifecycle.md) | Client、临时与既有实例、启动、复位、关闭 |
| [输入与外设](inputs.md) | 按键、板型输入、Wi-Fi、SD、摄像头、ADB、动作任务 |
| [BLE 中心端](ble.md) | 扫描连接、属性读写、通知、操作期限与清理 |
| [观测与采集](observations.md) | UART、截图、显示、信号、WAV 输入、DAC 录制 |
| [错误与清理](errors.md) | 期限、异常、显式证据、失败保留、回收 |
| [pytest 集成](pytest.md) | fixture、命令行选项、失败归属 |
| [固件测试示例](../examples/README.md) | 对应固件的启动、UID、业务判据与状态机 |

## 返回类型与单位

从 `lisem` 导入 `Client`、`Instance`、`Run`、`Delivery`、`Screenshot` 和
[异常类型](errors.md)。Instance 与 Run 通过客户端方法取得。

`Delivery` 数据对象，含 `status: str`、`raw: dict`。状态按操作分别为
`saved`（已保存）、`applied`（当前运行已应用）、`queued`（UART 排队）、
`dispatched`（动作已交付）、`completed` 或 `cancelled`。`raw` 保留后端确认
字段及标识；输入交付的业务效果由固件输出另行断言。

`Screenshot` 含 `png: bytes`、`metadata: dict`；保存方法见[观测与采集](observations.md)。
其他查询返回字典、列表或页面，各页列出关键字段。路径接受字符串或 `Path`；
`timeout` 以宿主秒计，虚拟时刻以 ns、按键及动作间隔以 ms 计。

应用的 `runtime/python-docs/` 含 `docs/`、`examples/`、[安装说明](../README.md)与
许可，可离线浏览相对链接。SDK 版本跟随产品，`source_info()` 返回模块来源及
配套构建信息；实际原生握手身份见 `client.build`。正式应用包从
[GitHub Release](https://github.com/LISTENAI/Lisem/releases) 获取。
