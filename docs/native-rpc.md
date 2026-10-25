# 原生 RPC v1

`lisem --data-dir LIBRARY rpc` 提供以标准输入输出传输的原生管理会话。
每行一个 UTF-8 JSON 请求或响应；标准输出不混入诊断。Python SDK 通过
该入口创建实例并发现 worker，运行中的控制和观察直接调用 worker，
不经过 MCP，也不为每次操作启动 CLI 子命令。

管理请求形如 `{"id":1,"method":"hello","params":{}}`。成功响应保留
`id` 并返回 `result`；错误保留 `error` 文本及 `error_kind`。
请求上限 64 KiB，SDK 响应上限 4 MiB。会话不自动恢复其他实例的 UART。

`hello` 返回 `protocol=lisem-native`、`version=1`、编译时 `build` 和
能力清单。SDK 在创建资源前检查协议及所需能力；已有 worker 的构建
身份必须与管理程序相同，不在查询时自动替换或停止旧 worker。

管理入口负责以下生命周期边界：

- `catalog` 返回板型、芯片与能力描述。
- `create` 复用现有 UID 校验、碰撞检查、OTP 写入和 LPK 导入。
- `create_temporary` 增加本地随机所有权标识，在实例正式进入设备库之前
  写入所有权记录。该标识独立于芯片 UID。
- `connect` 仅发现或启动指定实例的 worker，返回私有回环端点、worker
  身份和是否由此调用创建；认证 token 不应写入测试报告。
- `find_temporary` 根据所有权标识查找本库中的临时实例。
- `remove_temporary` 校验所有权、库内路径及 runtime/storage 租约；先
  退休存储目录并更新索引，再删除。运行或 worker 未退出时拒绝删除。
  删除或索引恢复失败须报告遗留路径，不能静默成功。

worker 端点继续使用现有协议版本 1，每个请求建立一条有界回环 TCP
连接，以端点 token 认证。请求包含 `method`、`params` 和 `token`。
参数中的 `run` 绑定实际启动轮次，`worker` 可额外绑定 worker 身份。
`shutdown` 的 `expected_idle=true` 防止关闭仍有活动运行的 worker。

错误种类 `stale_run` 表示旧运行，`delivery_unknown` 表示 QEMU 控制
仍可能完成，其他明确失败使用 `rejected`。连接在请求发出后丢失时，SDK
将修改操作报告为交付未知，不盲目重放。原有客户端仍可读取 `error` 文本。

状态中的 `pending` 需要后续身份匹配的确认；输入交付不代替固件业务
断言。UART、截图、WAV、信号与虚拟事件沿用当前后端的真实时序和有界
采集语义。新增不兼容行为须升级协议，向后兼容能力可在清单中增加。
