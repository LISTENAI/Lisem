# 使用说明

解压应用包后，macOS 打开 `Lisem.app`，Windows 打开 `lisem-desktop.exe`，
Linux 运行 `./lisem-desktop`。每个实例拥有独立 Flash、OTP 和 UID。

Linux 可在解压目录运行 `./install-desktop.sh`，将 Lisem 加入当前用户的应用
菜单。移动应用目录后重新运行脚本即可更新启动路径。

「关于 Lisem」中的「检查更新」查询 GitHub 最新正式版。有新版本时，点击
「查看新版本」打开发布页面，查看说明并下载。

## 实例与固件

实例库列出实例名称、板型和运行状态。顶部添加按钮用于新建实例或打开已有
实例目录；双击列表项打开设备，点击齿轮按钮打开设备设置。右键菜单和更多
操作菜单提供打开所在文件夹、移除等操作。

新建设备时选择板型，也可先[导入自定义板型 JSON](boards.md)。
可创建空白 Flash，或导入原始真机 LPK。LPK 只识别
芯片，板型需手动选择。导入会将 LPK 内容写入实例 Flash。实例菜单支持
复制/重新生成 UID、全片擦除及 LPK 写入，
这些存储操作要求设备下电。

移除实例仅从库中移除记录，不删除磁盘文件。已存在的实例可以重新打开。
实例的板型或芯片配置与当前版本不兼容时，启动会报错。

上电后固件执行 ROM 和 Flash；Arcs-Mini 固件可能要求长按功能键才能启动应用。
按住鼠标就是按住按键，松开即释放。Arcs-EVB 提供 K1、K2、K3 三个 ADC
按键，支持在屏幕上按下、拖动和松开进行单点触摸操作。
复位保留 Flash、OTP 和 UART 连接。
关闭窗口或退出应用后，实例继续在后台运行；需要停止时使用下电按钮。

### 写入原始二进制

使用原始镜像时，先创建目标板型的空白实例，再将文件写入 Flash：

```sh
lisem create --board BOARD_ID --name Test
lisem write INSTANCE_ID firmware.bin
lisem start INSTANCE_ID --seconds 30
lisem logs INSTANCE_ID 0
```

`BOARD_ID` 选择固件的目标板型，`INSTANCE_ID` 使用创建结果中的 ID。
`write` 默认从 Flash 偏移 0 写入，`--offset` 可指定其它字节偏移；写入前须下电。
多核固件按发布布局准备合并镜像，或将各段写到对应偏移。
串口、SD 镜像和其它运行设置使用本文对应的操作入口。

## 信号输入

从设备窗口的「实例」菜单展开「信号输入…」面板。数字量可切换高低电平；
模拟量可拖动滑块或用加减按钮调节；输入精确电压后，按回车或移开焦点
即可生效。修改时可同时观察设备画面和操作按键。
「恢复默认」使用板型声明的默认值，设置按实例保存。

## 音频和网络

右上角分别提供麦克风和扬声器入口。扬声器控制宿主声音输出，默认开启；
麦克风控制宿主音频输入。图标亮起表示固件侧启用对应输入或板级 PA，
与宿主开关分别显示。

麦克风可以在运行中开启或关闭。等待切换完成后即可使用；若失败，按提示检查
系统权限和设备连接。下电时的选择会保存，供下次上电使用。
Linux 录播需要可用的 PulseAudio 或 PipeWire pulse 服务；不启用音频的无头
运行没有此要求。自动化测试可通过 CLI/MCP 输入 16 kHz、单声道 PCM16 WAV；
该输入与宿主麦克风互斥。

摄像头、麦克风、扬声器、网络、SD 卡和串口可在右上角菜单或设置的
「连接与媒体」页调整。音视频菜单中的勾选表示当前输入或输出，选择「关闭」可停用。
Wi-Fi 菜单与设置中的「热点可用」模拟热点出现或消失，可在运行中切换。
固件自行检测掉线和重新连接。
GUI 上电时启用宿主网络桥接；无头 CLI/MCP 通过运行选项显式选择网络。

默认无线环境包含开放热点 `Lisem`。开启宿主网络后，固件连接启用互联网的
热点即可访问宿主网络。无头运行未启用宿主网络时，逻辑热点仅提供离线连接。

## Wi-Fi 环境

设备窗口右上角的 Wi-Fi 菜单提供「热点设置…」，可在运行中编辑并保存。
运行时显示每个热点的 BSSID 和连接状态；WPA2 完成认证后标为「已连接」。
该状态表示 Wi-Fi 链路建立，不代表互联网或业务服务可达。
每个实例最多配置 16 个热点，包含名称、RSSI、信道、安全方式、密码和
「互联网」开关。关闭互联网时仍可扫描、认证、连接和获取 IP，但热点不转发
外网流量。新增和删除热点模拟热点出现与消失；删除全部热点表示附近没有网络。
「热点可用」控制整个无线环境的出现与消失。

同名热点可配置为不同信号和信道。调整 RSSI、互联网开关或其它热点时保留
已有连接；修改热点名称、信道或安全配置时，该热点重新开始认证和关联。
修改后点击「保存」，等待页面显示「已保存」。

命令行读取当前环境或从 JSON 文件保存：

```sh
lisem wifi-environment INSTANCE_ID
lisem wifi-environment INSTANCE_ID --file wifi.json
```

```json
{"access_points":[
  {"ssid":"Lisem","rssi":-45,"channel":1,"internet":true},
  {"ssid":"Nearby","rssi":-80,"channel":6,"internet":false},
  {"ssid":"SecureLab","rssi":-50,"channel":11,"internet":true,
   "security":"wpa2-psk","password":"testing-password"}
]}
```

CLI 自动读取当前运行标识，也可用 `--run RUN_ID` 拒绝过期操作。MCP
的 `lisem_set_wifi_environment` 在运行中更新时须提供当前 `run`。

名称为 1–32 个 UTF-8 字节，RSSI 为 −127–0 dBm，信道为 1–14。
支持开放热点和 WPA2-Personal（AES-CCMP）。省略 `security` 时为开放热点；
WPA2 密码需为 8–63 个可打印 ASCII 字符。
不支持 WPA3、TKIP、企业认证或管理帧保护（PMF）。RSSI 和信道用于模拟扫描
结果，不模拟射频调谐、干扰或传播衰减。

## USB ADB

固件提供 USB ADB 接口时，可在设备窗口右上角的 USB 菜单或设置的
「连接与媒体」中开启 ADB 桥接，点击连接地址即可复制。图标亮起表示固件
USB 已完成配置。也可通过 CLI 开启，再使用宿主安装的 `adb`：

```sh
lisem usb-adb INSTANCE_ID --enable
adb connect 127.0.0.1:PORT
adb -s 127.0.0.1:PORT shell
```

将示例地址替换为命令返回的 `listen`。仅绑定宿主回环地址；每次开启自动
分配端口。查询状态使用 `lisem usb-adb INSTANCE_ID`，关闭使用
`lisem usb-adb INSTANCE_ID --disable`。CLI 自动取得当前运行标识，也可传入
`--run RUN_ID` 拒绝过期操作。实例下电后桥接关闭，下次运行需重新开启。

`enabled` 表示监听已开启，`connected` 表示 TCP 客户端已连接；固件 USB
就绪情况见 `status` 和 `error`。用 `adb devices` 确认设备可用后再操作。
可用的 shell 命令、日志位置和 `adb push` 目标路径取决于固件。
固件切换 USB 模式时，等待重新枚举，必要时重新执行 `adb connect`。

## 摄像头

GC0328 可使用本地 PNG、JPEG 或 PNM 图片作为静态场景。
在右上角摄像头菜单中选择「图片…」，也可运行
`lisem camera INSTANCE_ID image.png`；
`lisem camera INSTANCE_ID --clear` 清除输入。图片按板型配置的摄像头安装方向
转换，保持比例、居中裁切至 640×480；固件负责镜像、预览旋转、
传感器格式、裁切窗口和采集。运行中换图从下一帧生效。
控制暂时繁忙时会显示「正在确认输入切换结果…」，此时不要重复选图；确认成功
后才保存来源路径。若始终无法确认，运行会停止并明确保留结果未知的状态。
没有输入时传感器仍可识别和配置，但不生成帧，固件可能因此等待或超时。

实例只保存来源路径，下一次上电重新读取；文件失效会明确报错。图片至多
32 MiB，宽高各不超过 4096 像素。

macOS 和 Windows 可在摄像头菜单或实例设置中选择具体设备，或选择「关闭」。
「刷新设备…」重新查找摄像头，开启采集时需要系统相机权限。实例上电后，
所选摄像头持续采集画面。下电时的选择会保存，下一次上电使用；运行中可切换
图片、摄像头或关闭输入。摄像头断开或权限失败时，按提示排除问题后重新选择
设备即可重试；使用另一只摄像头时需手动选择。

命令行使用 `lisem camera-devices` 列出设备，再运行
`lisem camera INSTANCE_ID --device DEVICE_ID` 选择来源；`--clear` 同样可关闭
实时输入。MCP 对应 `lisem_camera_devices` 和 `lisem_camera_input` 的 `device_id`。
图片与实时取景均按板型配置的安装方向转换，固件负责镜像。Lisem 不自动录制
摄像头画面；拍照操作取决于所运行的固件。

下电或关闭输入会释放摄像头；仅关闭 GUI 窗口会保留后台实例和采集。
Linux 支持静态图片输入，暂不支持宿主摄像头。视频文件及 GC0328 的
光学和模拟 ISP 效果尚不支持。

## SD 卡

在右上角 SD 卡菜单或设置的「连接与媒体」页选择「插入镜像…」，打开已准备的
可写 raw 镜像；「弹出」模拟拔卡。菜单显示当前镜像文件名，运行中可直接切换。
镜像路径按实例保存，上电时重新打开原文件；固件写入直接保存在镜像中。

镜像大小、文件系统准备与 CLI 用法见[SD 卡镜像](automation.md#sd-卡镜像)。

## UART

右上角串口菜单直接启用或关闭各路 UART；「连接信息…」显示已启用端口的
连接路径，并提供「复制路径」。设置的「连接与媒体」页也可启用端口和复制路径。
先打开所需 UART，再连接端点，最后上电即可看到完整启动日志。
macOS/Linux 使用 PTY，可使用支持 TTY 路径的终端或串口工具，例如：

```sh
picocom --baud 115200 --imap lfcrlf /dev/ttysXXX
```

`--imap lfcrlf` 是 picocom 的显示选项，用于固件只输出 LF 的日志；模拟器
保留原始字节。Linux 的端口通常形如 `/dev/pts/3`。Windows 显示
`tcp://127.0.0.1:PORT`，可用支持原始 TCP 的终端工具连接，例如 PuTTY 的
**Raw** 模式；不要使用 Telnet 模式。
复位、上下电和退出 GUI 均保留端点，关闭该路 UART 或执行 `lisem shutdown` 才释放。

## 串口烧录

启用 UART0，在复位菜单中进入烧录模式，再运行支持不复位策略的 cskburn：

```sh
cskburn -C arcs -b 230400 -s /dev/ttysXXX --chip-id --reset-strategy none
```

具体擦写和 LPK 参数按当前 cskburn 帮助使用。不要同时让 picocom 和烧录
工具读取同一端口。macOS 虚拟 PTY 不传 DTR/RTS，因此烧录工具不能自动
控制板级 BOOT/RESET；烧录结束后从桌面正常复位。
Windows 的 TCP 端点不是 COM 串口，要求串口设备路径的 cskburn 不能直接
连接；可使用实例菜单或 CLI 导入 LPK。

## 无头命令行

从 Lisem 菜单选择「安装命令行工具」，即可设置终端命令。macOS 创建
`/usr/local/bin/lisem` 链接；Linux 创建 `~/.local/bin/lisem` 链接，若该目录
尚未在 PATH 中，按界面提示加入。Windows 将应用目录加入当前用户 PATH，
重新打开终端后生效。移动应用后重新安装命令可更新路径。

也可直接运行包内 CLI：macOS 为 `Contents/MacOS/lisem`，Windows/Linux
为包根目录的 `lisem.exe` / `lisem`。以下 `INSTANCE_ID` 取自创建结果：

```sh
lisem create --board arcs-mini --name "Mini" --lpk firmware.lpk
lisem create --board arcs-evb --name "EVB" --lpk arcs-evb.lpk
lisem list
lisem uid INSTANCE_ID
lisem uart INSTANCE_ID 0
lisem start INSTANCE_ID --seconds 120 --timeout 180
lisem button INSTANCE_ID function press
lisem button INSTANCE_ID function release
# Arcs-EVB 的三个 ADC 按键
lisem button INSTANCE_ID k1 press
lisem button INSTANCE_ID k1 release
lisem screenshot INSTANCE_ID screen.png
lisem logs INSTANCE_ID 0
lisem reset INSTANCE_ID
lisem stop INSTANCE_ID
lisem import INSTANCE_ID firmware.lpk
lisem erase INSTANCE_ID
lisem uid INSTANCE_ID --regenerate
lisem shutdown INSTANCE_ID
```

`button press/release` 是即时手动输入。自动化长按、连按使用一次提交的虚拟时间序列：

```sh
lisem button-sequence INSTANCE_ID function --run RUN_ID --count 3 --hold-ms 80 --gap-ms 80
lisem button-sequence-status INSTANCE_ID SEQUENCE_ID --run RUN_ID
lisem button-sequence-status INSTANCE_ID SEQUENCE_ID --run RUN_ID --cancel
```

`RUN_ID` 从 `lisem --json status INSTANCE_ID` 的 `runtime.session.output` 获取，
`SEQUENCE_ID` 由提交结果返回。次数为 1–32，总时长至多 60 秒，按模拟器的
虚拟时间执行，最后自动松开。即时手动输入取消当前序列并接管按键；复位、下电
终止序列。返回 `completed` 只表示输入完成，不保证固件业务成功。
`reset --download` 进入 ROM 烧录模式；`write --offset` 写入原始二进制。
`send INSTANCE_ID 0 TEXT` 向 UART 发送 UTF-8 字节，`--hex` 发送二进制。
默认输出列表、状态和操作结果；`--json` 输出结构化数据，供脚本读取；`--data-dir` 或 `LISEM_DATA_DIR` 选择独立实例库。

运行中可以通过 CLI 操作热点和音频：

```sh
lisem access-point INSTANCE_ID off
lisem access-point INSTANCE_ID on
lisem audio-input INSTANCE_ID input.wav
lisem audio-output INSTANCE_ID --capture output.wav
```

`access-point` 设置热点是否可用，配置会保存。
可加 `--run RUN_ID` 拒绝过期运行，否则自动读取当前运行标识。下电也可配置。
返回 `pending` 时需查询状态确认，不能当作已经切换成功。

`audio-input` 接受最长 24 小时的 16 kHz 单声道 PCM16 WAV，要求系统麦克风关闭。
按 1:1 实时速度注入；模拟器运行速度不足或固件暂停采样可能延长等待。
命令阻塞至实际消耗完毕，Ctrl+C 取消剩余输入，设备继续运行。默认宿主等待
上限 90 秒，可用 `--timeout` 调整；超时取消剩余输入并返回错误。返回的
`completed` 仅表示音频输入完成，不表示业务识别成功。

`audio-output --capture` 显式开启侧录，Ctrl+C 结束并封好 WAV，设备继续运行。
录制原始 DAC 输出，独立于宿主静音和板级 PA 开关；按虚拟时间补齐 DAC 停止
期间的静音。既有输出文件不会覆盖。录制期间采样率变化、读取积压、运行结束
或重置会中断录制并报错，已写入的 WAV 尽可能保留。录制命令异常退出后，
侧录在 5 秒内停止。

前台 `run` 有明确虚拟时间和宿主时间上限，Ctrl-C 停止本轮运行；失败返回
非零退出码。后台 `start` 在 CLI 返回后继续运行，`stop` 保留 UART 端点，
`shutdown` 同时关闭运行时和 UART 端点。每个实例独立，可以分别启动和控制。

CLI 默认离线、无宿主音频；`--network` 启用宿主上联，`--audio` 用于
播放、`--microphone` 同时采集，`--mute` 仅静音实际输出。无头模式不要求
DISPLAY、麦克风或声音设备，适合文本终端和 CI。

正常运行不保存录音、画面、UART 或网络包的归档。需要排查问题时，显式使用
`lisem run INSTANCE_ID --capture DIRECTORY`；诊断内容保存在指定目录，
可能包含原始语音和固件日志，由调用方管理。

开发时 `make headless` 只构建 Rust CLI；从源码目录运行还需
`make qemu-build`，启用网络另需 `python3 tools/build_network.py`。

MCP 的接入方式见 [Coding agent 接入](agents.md)。
