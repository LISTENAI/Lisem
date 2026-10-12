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
