# VenusA RD EVB

`venusa-rd-evb` 使用 CSK7004J8U：双核 Nuclei CPU、512 KiB SRAM、
16 MiB Flash 和 8 MiB PSRAM。实例从固定芯片 ROM 启动，Flash 和 OTP
分别持久化；设备 UID 来自实例 OTP。

OTP 的 64 字节原始数据同时映射到 eFuse 自动加载区和直接读取区。
VenusA 的生产 UID 字段位置及编码尚未确认；`lisem uid` 提供的虚拟实例标识
不能作为真机 UID 读取兼容性的验证依据。

桌面提供运行状态、UART 和 SD 卡操作。该板型不连接屏幕、摄像头或触控器件。
宿主麦克风及交互式音频输入不在当前运行控制能力中。

## 启动 SDK 固件

准备为 `venusa_rd_evb` 编译的原始二进制，创建实例并写入 Flash：

```sh
lisem create --board venusa-rd-evb --name VenusA
lisem write <id> build/arcs.bin
lisem start <id> --seconds 30
lisem logs <id> 0
```

`<id>` 使用创建结果中的实例 ID。SDK 的输出文件名可能仍为 `arcs.bin`；
应核对编译目标，不能依据文件名判断芯片。双核固件使用带有各核镜像布局的
合并二进制，不能把从核镜像单独写到 Flash 起始位置。

`start` 将实例留在后台；`stop` 下电，`shutdown` 关闭实例运行时。
UART 通道 0、1、2 可独立连接，字节原样转发。具体接线和协议由固件设置。

## SD 卡

SD 卡使用可写 raw 镜像，支持 8 MiB 至 32 GiB 的 2 次幂容量。需要文件系统的
固件应使用已格式化的镜像，并自行准备所需文件。原始扇区测试会修改指定扇区，
应使用独立测试镜像。

```sh
lisem sd-card <id> --image card.img
lisem start <id> --seconds 30
lisem sd-card <id> --eject
```

CLI 和 GUI 自动携带当前运行标识；脚本可用 `--run` 显式指定标识并拒绝
过期请求。
镜像由实例独占写入。板载卡座的 CD 脚未连接，插拔不生成机械检测边沿；
固件通过实际 SD 命令和 I/O 判断介质状态。

## 控制器与器件

CPU、ECLIC、CIDU、时钟、UART、DMA、GPIO、PinMux、Flash、PSRAM、SD、
定时器、看门狗、RTC、GPADC、SPI、I2C、APC、I2S、Codec、DVP、DMA2D
和 RGB 输出由 QEMU 建模。
未实现的寄存器模式明确报错，控制器传输完成不代表外接器件的功能已验证。
SPI 支持常规全双工主机和从机传输；从机收发需要实际 CS、SCLK 和数据
连线。双线和四线数据模式明确拒绝。

I2C 和 SPI 外接器件通过实际总线事务通信。例如开发测试可在 I2C 总线上
连接 QEMU 的 AT24C EEPROM；空总线不会提供 EEPROM、触控或传感器数据。
I2S 外部串行时钟和音频器件同样需要明确的输入连接。开发验证可使用独立的
I2S 对端、GC0328 摄像头和 FT6236 触控器件；它们通过数字信号、实际总线、
FIFO、DMA 和中断与固件通信，不改变板型默认连接。

RGB 输出可显式捕获完整帧用于像素验证。板型没有屏幕器件时，桌面不显示
虚构的屏幕；DVP 场景输入也需要明确连接传感器。

Codec 场景输入支持单声道或双声道 PCM16，双声道分别对应两颗麦克风；
ADC 采样和 DAC 输出仍由固件配置。显式 WAV 诊断保留实际 DAC 样本，
不以提示语、测试结束日志或寄存器回读替代音频数据验证。

## 验证

```sh
make qemu-venusa
```

该入口运行有界 QTest，覆盖芯片中断、存储、时钟与复位、ADC、音频、PWM、
SPI、I2C 和 DMA 等数字行为。SDK 用例的验证还需准备原始固件及其资源，
核对实际 UART、内存、器件输出或媒体数据。依赖外部器件的用例必须说明
接线和输入条件；仅启动或打印结束消息不能代替功能断言。
