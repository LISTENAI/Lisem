# 使用说明

通过 `make build` 构建，再用 `make run` 或打开 `artifacts/desktop/Lisem.app`
启动；Windows/Linux 打开 `artifacts/desktop/Lisem/` 中的桌面程序。
应用保存实例列表；每个实例拥有独立 Flash、OTP 和 UID。

Linux 可在解压目录运行 `./install-desktop.sh`，将 Lisem 加入当前用户的应用
菜单。脚本按 XDG 目录安装图标和启动项；移动应用目录后重新运行即可更新路径。

## 实例与固件

新建设备时选择板型。可创建空白 Flash，或导入原始真机 LPK。LPK 只识别
芯片，板型需手动选择。导入会将 LPK 内容写入实例 Flash。实例菜单支持
复制/重新生成 UID、全片擦除及 LPK 写入，
这些存储操作要求设备下电。重新生成 UID 可能需要在业务平台重新登记。

移除实例仅从库中移除记录，不删除磁盘文件。已存在的实例可以重新打开。
上电前核对实例的硬件描述；板型或芯片配置不匹配时拒绝启动。
显示名称和控件标签不参与硬件兼容性判断。

上电后固件执行 ROM 和 Flash；原固件可能要求长按功能键才能启动应用。
按住鼠标就是按住按键，松开即释放。复位保留 Flash、OTP 和 UART 连接。
关闭窗口或退出应用后，实例继续在后台运行；需要停止时使用下电按钮。

## 音频和网络

声音默认开启。右上角声音图标控制宿主静音，图标反色表示板级 PA 已使能。
实例设置中的麦克风开关在下电时修改，使用时需允许系统麦克风访问。Linux 录播需要可用的 PulseAudio 或
PipeWire pulse 服务；不启用音频的无头运行没有此要求。
不用麦克风时仍实时播放 DAC，可导入 16 kHz、单声道 PCM16 WAV 测试输入。

开启宿主网络后，固件可连接开放 AP `Lisem` 并访问宿主网络。固件的设备
鉴权、资源更新和唤醒词均由其业务及平台配置决定，模拟器不代办。关闭宿主
网络仍有离线逻辑 AP，但不提供互联网。

## 摄像头

Mini 的 GC0328 可使用本地 PNG、JPEG 或 PNM 图片作为静态场景。在实例连接
设置的“摄像头输入”下拉框中选择“图片…”，也可运行
`lisem camera INSTANCE_ID image.png`；
`lisem camera INSTANCE_ID --clear` 清除输入。图片按 Mini 摄像头的安装方向
顺时针旋转 90°，保持比例、居中裁切至 640×480；固件仍负责镜像、预览旋转、
传感器格式、裁切窗口和采集。运行中换图从下一帧生效。
控制暂时繁忙时会显示“正在确认图片切换结果”，此时不要重复选图；确认成功
后才保存来源路径。若始终无法确认，运行会停止并明确保留结果未知的状态。
没有输入时传感器仍可识别和配置，但不生成帧，固件可能因此等待或超时。

实例只保存来源路径，下一次上电重新读取；文件失效会明确报错。图片至多
32 MiB、宽高各不超过 4096 像素；解码和帧缓冲有固定上限，默认不录制图片。

macOS 还可在同一下拉框中选择具体的宿主摄像头，或选择“关闭输入”。
“刷新设备…”只枚举设备，实际开启采集时
才请求系统相机权限。实例上电后，所选摄像头持续采集最新画面，供固件随时取景或拍照。
下电时选择的设备会保存，下一次上电使用；运行中可在
图片和摄像头之间切换，也可关闭输入。设备选择按标识保存，不会自动换到另一只
摄像头。断开或权限失败会显示原因；排除问题后重新选择设备即可重试。

命令行使用 `lisem camera-devices` 列出设备，再运行
`lisem camera INSTANCE_ID --device DEVICE_ID` 选择来源；`--clear` 同样可关闭
实时输入。MCP 对应 `lisem_camera_devices` 和 `lisem_camera_input` 的 `device_id`。
实时取景沿用 Mini 安装方向，固件的自拍镜像继续生效。采集只在内存中传递最新
画面，不自动录制；固件主动拍照、联网和上传仍由固件业务决定。

下电或关闭输入会释放摄像头；仅关闭 GUI 窗口会保留后台实例和采集。
Windows/Linux 暂不支持宿主摄像头，仍可使用静态图片。视频文件及 GC0328 的
光学和模拟 ISP 效果尚不支持。

## UART

先打开所需 UART，再连接显示的端点，最后上电即可看到完整启动日志。
macOS/Linux 使用 PTY：

```sh
picocom --baud 115200 --imap lfcrlf /dev/ttysXXX
```

`--imap lfcrlf` 是 picocom 的显示选项，用于固件只输出 LF 的日志；模拟器
保留原始字节。Linux 的端口通常形如 `/dev/pts/3`。Windows 显示
`tcp://127.0.0.1:PORT`，用 PuTTY 的 **Raw** 模式连接，或复制界面给出的
`putty -raw 127.0.0.1 -P PORT` 命令；不要使用 Telnet 模式。
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
lisem list
lisem uid INSTANCE_ID
lisem uart INSTANCE_ID 0
lisem start INSTANCE_ID --seconds 120 --timeout 180
lisem button INSTANCE_ID function press
lisem button INSTANCE_ID function release
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
`SEQUENCE_ID` 由提交结果返回。次数为 1–32，总时长至多 60 秒；所有边沿由 QEMU
虚拟时钟调度，最终自动松开。即时手动输入取消当前序列并接管按键；复位、下电
终止序列。返回 `completed` 只表示输入完成，不保证固件业务成功。
`reset --download` 进入 ROM 烧录模式；`write --offset` 写入原始二进制。
`send INSTANCE_ID 0 TEXT` 向 UART 发送 UTF-8 字节，`--hex` 发送二进制。
默认输出列表、状态和操作结果；`--json` 输出结构化数据，供脚本读取；`--data-dir` 或 `LISEM_DATA_DIR` 选择独立实例库。

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
