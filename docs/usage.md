# 使用说明

[下载与安装](../README.md#快速开始) · [Python SDK](../python/README.md) ·
[Coding agent 接入](agents.md) · [自动化运行](automation.md)

## 首次运行

解压应用包，macOS 打开 `Lisem.app`，Windows 打开 `lisem-desktop.exe`，
Linux 运行 `./lisem-desktop`。Linux 可运行包内 `./install-desktop.sh` 添加
应用菜单入口，移动应用目录后需重新运行。

1. 点击实例库右上角的添加按钮，选择「新建实例…」。
2. 选择目标板型，导入自行准备的 LPK；也可创建空白 Flash 的实例。
3. 双击打开设备并上电。部分固件需要长按功能键才能启动应用。

设备窗口提供按键与外设入口，设置在对应设备窗口中打开。窗口宽度不足时，
部分入口会收进「更多」菜单。Windows/Linux 的应用菜单位于实例库标题栏。
「关于 Lisem」中的「检查更新」可查看新版本。

## 实例与固件

每台实例独立保存 Flash、OTP、UID 和设置。创建时选择板型，也可先
[导入自定义板型 JSON](boards.md)。

UID 可自行填写，留空时自动生成。

在设置的「身份与存储」页写入 LPK、清空 Flash 或重新生成 UID，操作前须
下电。清空 Flash 保留 UID 和其它 OTP 内容。移除实例仅移除库中记录，
磁盘文件可通过添加按钮重新打开。

### 上电、复位与下电

按住鼠标即按住设备按键，松开即释放；触摸屏支持按下、拖动和松开的单点
操作。复位保留 Flash、OTP 和 UART 连接。

**关闭窗口或退出 GUI 后，设备仍在后台运行。** 需要停止设备时使用下电
按钮。

### 写入原始二进制

先创建目标板型的空白实例，再按发布布局写入镜像：

```sh
lisem create --board arcs-mini --name Test
lisem write INSTANCE_ID firmware.bin
```

`INSTANCE_ID` 使用创建结果中的 ID。写入须下电，默认 Flash 偏移为 0；
多段镜像通过 `write --offset` 指定各段偏移。

## 麦克风与扬声器

在设备窗口右上角或设置的「连接与媒体」页选择输入与输出。扬声器播放默认
开启，麦克风可在运行中切换。
图标亮起反映固件侧音频输入或板级 PA 状态，宿主开关单独显示。

Linux 录播需要 PulseAudio 或 PipeWire pulse 服务。自动化可注入 16 kHz、
单声道 PCM16 WAV，与宿主麦克风互斥；录制和注入命令见
[音频证据](automation.md#wav-注入与音频证据)。

## Wi-Fi 环境

桌面上电后，固件可通过默认的开放热点 `Lisem` 访问电脑所在网络。

从 Wi-Fi 菜单打开「热点设置…」，配置开放或 WPA2-Personal 热点，编辑后
点击「保存」。

- 「热点可用」控制整个无线环境的出现或消失。
- 每个热点的「互联网」控制外网访问，关闭后仍可连接热点。

CLI 使用 `lisem wifi-environment INSTANCE_ID` 读取配置，将结果保存为 JSON
并编辑后，可用 `--file wifi.json` 写回。参数见命令的 `--help`。
无头运行需显式启用宿主网络，见[无头命令行](#无头命令行)。

## 摄像头

在摄像头菜单或「连接与媒体」中选择 PNG、JPEG、PNM 图片或具体摄像头，
运行中可切换来源，选择「关闭」停止输入。使用摄像头需授予系统相机权限，
找不到设备时可点击「刷新设备…」。

实例记住所选来源，下次上电重新打开；使用图片时需保留原文件。
图片规格、Linux 设备要求和 CLI 操作见[摄像头输入](automation.md#摄像头输入)。

## SD 卡

在 SD 卡菜单或「连接与媒体」中选择「插入镜像…」或「弹出」，运行中可
切换。使用 8 MiB～32 GiB、大小为二次幂的可写 raw 镜像，先准备固件支持的
分区和文件系统；固件写入直接保存到镜像。

镜像路径按实例保存，同一镜像不能同时由多个实例或宿主工具写入。传输中
弹出可能损坏文件系统。准备方法和 CLI 用法见
[SD 卡镜像](automation.md#sd-卡镜像)。

## UART

在串口菜单启用所需通道，通过「连接信息…」复制路径。先连接端点再上电，
即可接收完整启动日志。macOS/Linux 使用 PTY，例如：

```sh
picocom --baud 115200 --imap lfcrlf /dev/ttysXXX
```

替换为界面显示的路径；Linux 通常形如 `/dev/pts/3`。`--imap lfcrlf` 用于
显示只输出 LF 的日志。Windows 使用 `tcp://127.0.0.1:PORT`，可用 PuTTY
的 **Raw** 模式连接。
复位、上下电及退出 GUI 均保留端点，关闭通道或 `lisem shutdown` 才释放。

## USB ADB

固件提供 ADB 时，在 USB 菜单开启桥接并复制连接地址，使用宿主安装的 adb：

```sh
adb connect 127.0.0.1:PORT
adb devices
adb -s 127.0.0.1:PORT shell
```

地址使用界面显示值。CLI 可通过 `lisem usb-adb INSTANCE_ID --enable` 开启，
返回 `listen` 地址；`--disable` 关闭。下电后桥接关闭，下次运行需重新开启。
用 `adb devices` 确认可用后再操作；固件切换 USB 模式后可能需要重新连接。

## 信号输入

从「更多」菜单打开「信号输入…」，切换数字电平或调整电压。精确电压输入
在回车或移开焦点后生效；「恢复默认」恢复板型默认值。设置按实例保存。
CLI 操作见[信号输入](automation.md#信号输入)。

## 数字信号时序

通过 CLI 或 MCP 采集数字信号变化及虚拟时间，步骤见
[数字信号时序](automation.md#数字信号时序)。
Python 的采集接口见[数字信号](../python/docs/observations.md#数字信号)。

## 无头命令行

在应用菜单选择「安装命令行工具」，按提示设置 PATH；Windows 需重新打开
终端，移动应用后需重新安装命令。也可直接运行包内 CLI：macOS 为
`Contents/MacOS/lisem`，Windows/Linux 为包根目录的 `lisem.exe` / `lisem`。

```sh
lisem create --board arcs-mini --name Mini --lpk firmware.lpk
lisem list
lisem run INSTANCE_ID --seconds 30 --timeout 120
```

`INSTANCE_ID` 使用创建结果中的 ID。`run` 在前台运行，Ctrl+C 停止本轮；
`start` 在后台运行，配合 `status`、`stop` 控制，`shutdown` 还释放 UART
端点。无头运行默认离线且不打开宿主音频；添加 `--network` 启用宿主上联，
`--audio` 播放声音，`--microphone` 同时采集。

用 `lisem --help` 或 `lisem COMMAND --help` 查询命令与参数。
`--json` 输出结构化结果；`--data-dir` 或 `LISEM_DATA_DIR` 选择独立实例库。
长按、连按和条件动作使用虚拟时间编排，见[自动化运行](automation.md)。

需要诊断记录时，为 `run` 添加 `--capture DIRECTORY`。
脚本与 pytest 见 [Python SDK](../python/README.md)，agent 配置见
[MCP 接入](agents.md)。
