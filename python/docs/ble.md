# BLE 虚拟中心端

[API 总览](README.md) · [错误与清理](errors.md) · [探索示例](../examples/ble.py)

`run.ble` 连接当前 Arcs 固件提供的 peripheral / GATT server，支持扫描、
属性发现、bytes 读写和通知观察。固件需先通过自身的开机、按键或业务入口
开始广播；重新进入广播模式也由固件决定。

当前支持单连接、legacy 广告、1M、明文链路，ATT MTU 为 23。
`run.ble.capabilities()` 查询角色、安全方式和容量。SMP 配对、链路加密、
额外 PHY 和隐私地址解析尚未支持，对端要求这些能力时报告中断。

## 扫描并发现属性

```python
with run.ble as central:
    advert = central.scan().expect(
        lambda e: e["kind"] == "advertisement" and e["pdu_type"] == 0,
        timeout=10,
    )
    with central.connect(
        advert["address"], address_type=advert["address_type"], timeout=10,
    ) as connection:
        services = connection.discover()
        characteristics = connection.discover("characteristics")
        print(services, characteristics)
```

`scan(timeout=5)` 返回事件观察器；timeout 限制启动扫描的提交时间，扫描持续到
连接或中心端关闭。事件包含原始广告及扫描响应，`pdu_type=4` 为扫描响应，
`value` 为广告数据 bytes，`pdu` 为完整 PDU。地址为 `XX:XX:XX:XX:XX:XX`，
address_type 为 `public` 或 `random`。connect 收到实际对端链路响应后返回连接。

服务发现返回 `handle`、`end_handle`、`uuid`；特征发现返回 `handle`、
`value_handle`、`properties`、`uuid`。UUID 使用标准 16 位或 128 位字符串。
重连后重新发现属性，并使用新连接对象执行后续操作。

## 读写与通知

| 调用 | 返回及作用 |
| --- | --- |
| `connection.discover(kind="services", *, start=1, end=65535, timeout=10)` | 属性列表；kind 为 services、characteristics 或 descriptors |
| `connection.read(handle, *, timeout=10)` | 属性值 bytes |
| `connection.write(handle, value, *, response=True, timeout=10)` | 操作结果字典；value 必须为 bytes |
| `connection.subscribe(value_handle, cccd_handle, *, indications=False, timeout=10)` | 写入 CCCD，返回本连接、本属性的通知观察器 |
| `connection.notifications(*, after=None)` | 本连接全部通知和 indication 的观察器 |
| `connection.disconnect(*, timeout=10)` | 活动链路发送终止 PDU 并等待确认；已结束的同一连接返回停止状态 |

read 使用 Read Blob 读取后续片段，最长 512 字节。write 默认等待 ATT 响应，
长值使用 Prepare / Execute Write，最长 512 字节；response=False 使用最长
20 字节的 Write Command，返回链路交付确认。业务结果由调用方解析固件输出。

描述符发现使用 Find Information，返回所选范围内的全部属性。按特征及所在
服务的发现边界查找 CCCD，再传入 subscribe。观察器从写入 CCCD 前开始记录，
通过 `read(timeout=5)` 读取下一页，或 `expect(predicate, timeout=10)` 等待通知；
事件中的 value 为 bytes，handle 为实际属性句柄。

属性权限由固件决定。实际 ATT 错误抛出 `ATTError`，保留 opcode、handle、code
和完整 result；截断响应及未支持的传输通过异常报告。

## 异步操作与期限

`connection.submit(action, timeout=10, **params)` 提交 discover、read、write、
subscribe 或 disconnect，返回 `BLEOperation`。各 action 的参数如下：

| action | params |
| --- | --- |
| `discover` | `kind` 为 services、characteristics 或 descriptors；`start=1`、`end=65535` 限定范围 |
| `read` | `handle` 为当前连接发现的属性句柄 |
| `write` | `handle`、十六进制字符串 `value`，可选 `response`，默认为 `True` |
| `subscribe` | `handle` 为 CCCD 句柄；`value="0100"` 订阅通知，`"0200"` 订阅 indication |
| `disconnect` | 无附加参数 |

此接口的 `value` 为十六进制字符串，`operation.wait()` 返回原始结果字典；
同步 `read()`、`write()` 使用 bytes。例如：

```python
operation = connection.submit("read", handle=value_handle, timeout=10)
result = operation.wait(timeout=5)
value = bytes.fromhex(result["value"])
```

| 操作接口 | 期限含义 |
| --- | --- |
| `connection.submit(..., timeout=10)` | 设置原生操作期限，从原生受理开始计时；提交 RPC 另有最多 5 秒预算 |
| `operation.status(timeout=5)` | 查询该操作的当前结果 |
| `operation.wait(timeout=10)` | 设置本次等待的观察期限 |
| `operation.cancel(timeout=5)` | 确认取消待完成操作；已结束操作保持原结果 |

同步 connect、发现、读写、订阅和 disconnect 共用一个绝对期限，覆盖提交、
RPC、锁等待、解码和轮询。expect 的期限还覆盖条件匹配；匹配函数应保持简短。
BLE timeout 接受大于 0、至多 120 的宿主秒数。空口事件由 QEMU 虚拟时钟排程。

操作结果的 status 为 pending、response、delivered、connected、disconnected、
error、timeout、cancelled 或 interrupted。每个连接只允许一个待完成操作；
新操作替换已结束操作后，旧 operation 标识失效。

## 断开与清理

连接和操作对象属于创建它们的本次运行。复位、重新扫描或重连后，旧连接
对象会被拒绝。同一有效连接已经远端结束时，`disconnect()` 可以确认其
停止状态；其它拒绝和未知错误仍向调用方报告。

待完成操作的原生超时或确认取消会停止剩余传输，对端通过监督超时观察链路
丢失。取消不回滚已交付或正在完成的 PDU；交付未知的写操作须先核对原请求。
重试前确认固件已恢复广播，再扫描并建立新连接。

连接 with 上下文退出时，先取消待完成操作，再确认断开；主清理预算为 5 秒。
观察超时或 Ctrl+C 后，操作另有最多 5 秒确认取消，这段时间不计入观察 timeout。
取消未确认时，在原异常的 lisem_cleanup_error 中附加 DeliveryUnknown。
其它清理错误也保留原异常，并附加到同一属性。

`central.close(timeout=5)` 关闭自有中心端并停止其传输，对端通过监督超时观察
链路丢失。Run 上下文管理自有运行，临时设备及异常退出后的资源回收见
[生命周期](lifecycle.md)和[错误与清理](errors.md)。

## 事件与诊断

`central.events(after=None, timeout=5)` 创建全部事件的观察器；after 省略时从
当前游标开始，传入已知游标可继续读取。事件携带实例、run、peer、connection、
序号、虚拟时间、方向、协议层和原始值。核心保留 512 条历史，每页最多 128 条。
观察器保留当前页未消费的事件；历史溢出或底层传输丢失抛出 HistoryLost。

`central.status(timeout=5)` 查询链路与当前操作，不返回整个历史。
`central.trace(True)` 显式把原始双向链路 PDU 加入有界内存事件，trace(False)
关闭。需要保存时由调用方将读取的事件写入证据文件；事件原始值可能包含凭据，
普通 SDK 操作记录不保存 BLE 原始值。正常运行不自动写入 BLE trace。
