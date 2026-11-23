# UART 蜂窝模组

ML307C-GC-CN 模型通过板型 JSON 按需安装，接线见[板型说明](../boards.md#uart-蜂窝模组)。
模型提供理想蜂窝注册环境和真实宿主 IPv4 TCP／UDP 转发，固件仍执行 UART
驱动、AT 协议、TLS 及应用协议。注册或 PDP 激活不证明互联网、云鉴权或业务成功。

## 运行与 SIM

无头运行须显式使用 `start --network`；GUI 上电启用宿主网络桥接。
未启用桥接时，DNS 和 socket 打开返回错误。Wi-Fi 热点互联网开关不控制模组。

```sh
lisem modem INSTANCE_ID cellular --run RUN_ID
lisem modem INSTANCE_ID cellular --run RUN_ID --sim-present false
lisem modem INSTANCE_ID cellular --run RUN_ID --sim-present true
```

`cellular` 是板型器件 ID，`RUN_ID` 来自当前运行状态。每轮上电初始有 SIM。
SIM 改动只属于当前运行，不保存到板型或实例；拔卡解除 PDP 并关闭宿主 socket。
当前仅接 TX/RX，固件触发的 MCU 暖复位不切断独立模组的电源，保留其速率
和 SIM 状态。停止运行再重新上电会新建模组。
`AT+CPIN?` 可查询缺卡，`AT+CEREG?` 可查询注册状态。尚未实现有依据的该型号
SIM 热插拔 URC；不会伪造其它型号的通知。固件是否检测拔卡、重新初始化或选择
Wi-Fi 由其自身实现决定。观察状态不会推进虚拟时间。

状态返回 `sim_present`、`registered`、`pdp_active`、`host_network`、`baud`、
`connected_sockets`、`rx_bytes`、`tx_bytes` 和 `serial_overflow`。
`rx_bytes`／`tx_bytes` 分别是模组接收／交付 UART 的字节数。

## 协议范围

支持 `AT`、`ATE0/1`、`ATI`、`CGMM`、`CGMI`、`CGMR`、`IPR`、`CPIN?`、
`CEREG`、`CSQ`，以及 `MIPCALL`、`MDNSGIP`、`MIPCFG`、`MIPSTATE`、
`MIPOPEN`、`MIPSEND`、`MIPRD`、`MIPCLOSE` 的 IPv4 缓存模式子集。
模组版本查询明确返回 Lisem 子集身份。未知命令、TLS offload、IPv6、透传模式
及其它不支持的参数返回错误。未实现 RF、运营商认证、短信、语音通话、GNSS
及电源控制脚的电气时序。

Socket ID 为 1～5。TCP 单次发送最多 1460 字节、读取最多 4096 字节；UDP
单次发送最多 730 字节、接收单包最多 1460 字节，`MIPRD=id,1` 读取一个数据报。
二进制负载按声明长度传送，内容不作为 AT 命令解析。UDP 接收保留数据报边界，
只接收当前连接对端的数据；读取使用不带源地址的响应格式。

每个 TCP 方向最多缓存 32 KiB；UDP 接收最多 16 包；UART 输出最多 64 KiB。
串口积压保持背压，输出溢出明确标记错误，须重新上电后使用。
`MIPSEND` 确认表示模组接受了数据，不表示远端已处理。连接、DNS 和发送负载
有有界宿主期限；宿主异步请求有数量上限，关闭或模组复位后的旧请求不能复活连接。
TLS 明文及密钥不由模组解释，业务成功须根据真实网络响应单独验证。
