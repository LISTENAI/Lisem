# 错误、期限与清理

[API 总览](README.md) · [生命周期](lifecycle.md)

## 异常

异常可从 `lisem` 导入，均继承 `LisemError`。`StaleRun` 继承 `Rejected`，
`ObservationTimeout` 继承 `OperationTimeout`；后者也继承 Python `TimeoutError`。

| 异常 | 含义与处理 |
| --- | --- |
| `CompatibilityError` | 协议、构建、能力或平台不匹配，选择匹配应用 |
| `ProtocolError` | 响应格式错误或超限，保留协议现场 |
| `Rejected` | 后端明确拒绝，修正前置条件 |
| `StaleRun` | 运行或连接标识过期，使用当前 Run 及其新连接 |
| `ATTError` | 固件返回 ATT 错误，检查 `opcode`、`handle`、`code` 和 `result`，见 [BLE](ble.md) |
| `RunEnded` | 预期完成前运行结束，检查预算及固件输出 |
| `ObservationTimeout` | 总观察期限耗尽，检查 observation 和 cause |
| `OperationTimeout` | RPC 或有界等待失败，检查期限、条件及取消结果 |
| `DeliveryUnknown` | 修改可能已交付，保留标识并核对原请求，避免重复输入 |
| `HistoryLost` | 所需历史缺失或溢出，从明确初态重新完整观察 |
| `CleanupError` | 停止、证据或清理无法确认，检查附加错误及日记 |

非法参数和本地文件也可抛出标准 Python 异常。SDK 对修改请求不自动重放。

## 观察期限

`run.wait(predicate, *, timeout=10, description=None)` 和
`watch.expect(pattern, *, timeout=10, regex=False)` 的 timeout 为有限正数。
两者共用一个单调时钟截止时刻，覆盖 worker 锁等待、RPC、匹配与轮询；单次
RPC 最多 5 秒。wait 返回满足谓词的状态，expect 返回匹配末尾以前的缓冲字节。

总期限耗尽为 `ObservationTimeout`；尚有总预算的 RPC 失败仍为
`OperationTimeout`。异常 `observation` 含 `kind`（`deadline` / `rpc`）、
`run`、`target`、`phase`；UART 另含 `channel`，底层异常通过 `__cause__` 保留。
谓词和正则同步执行，返回后检查期限；自身异常原样传播。谓词保持简短，
内部操作设置各自期限，宿主调度不提供硬实时保证。

其他 timeout 按方法定义控制相应阶段：start 为整轮预算，按键/WAV 为完成
等待，set_inputs 先提交再等待确认，动作任务在轮询间检查期限。提交、取消、
停止及排空可能另需有界 RPC 时间，不能一概视为整次调用的硬性总耗时。

## 证据与清理

临时实例设置 `evidence_dir` 时，目录须为空且位于 `devices/` 外。退出时保存
构建身份、运行状态、操作、UART、可用截图和已启动的信号采集。常用输出为
`context.json`、每轮 `status.json`、`operations.json`、原始串口和截图。
也可调用 `device.save_evidence(directory, *, failure=None)` 或
`run.save_evidence(directory)`，为每次调用使用新目录。

原始 UART 含发送、回显及历史字节；涉及凭据时使用受控目录，共享前生成脱敏
副本。pytest 自动归属各阶段失败，其他框架调用 `client.mark_failed(reason)`。
原异常与清理错误同时发生时保留原异常，清理错误见 `lisem_cleanup_error`
属性；Python 3.11+ 也通过异常 note 提示。

## 异常退出后的回收

使用原设备库执行 `client.recover()`，返回每条日记的 `record`、`status`，
保留时含 `error`。`removed` 表示确认停止并删除，`absent` 表示资源已不存在，
`live` 表示活动客户端仍持锁，`retained` 表示身份或状态不明、保留待检查。

回收核对所有权、实例、worker、Run 与后端租约，不能按 PID 猜测或强删未知
资源。进程强杀后后台仍受预算约束，创建或停止交付未知时保留现场。临时实例
与有效日记回收需要 POSIX 文件锁，Windows 尚不支持。
