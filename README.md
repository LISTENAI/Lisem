# Lisem

Lisem 是面向聆思芯片平台的设备模拟器（emulator），提供桌面与无头命令行
两种操作方式。它运行原始真机固件，提供屏幕、按键、串口、网络和宿主音频，
让应用开发与调试不依赖始终连接开发板。

当前支持 **LS2684（ARCS）和 Arcs-Mini**，运行后端为 QEMU，桌面使用
Rust + GPUI Kit，提供 macOS、Windows 和 Linux 原生构建。CLI 不依赖
桌面会话，可在纯文本 Linux 和 CI 中运行。

## 构建与启动

需要 Python 3.10+、Rust 1.95.0 和本机编译工具链。各平台依赖及 Windows
构建环境见[开发说明](docs/development.md)。脚本下载并核对固定版本的
QEMU 与 libslirp；首次构建需要网络。

```sh
make build
make run
```

macOS 生成 `artifacts/desktop/Lisem.app`；Windows 和 Linux 生成
`artifacts/desktop/Lisem/`，运行其中的 `lisem-desktop.exe` 或
`lisem-desktop`。整个包包含 CLI、QEMU、音频、网络、硬件描述及非系统
动态库，应整体复制。运行不依赖源码、Python、Rust 或开发工具链。
macOS 本地包使用 ad-hoc 签名；公开分发还需 Developer ID 签名和公证。

在实例库中新建设备，选择 Arcs-Mini，导入真机 LPK 后上电。Flash、OTP
和 UID 随实例保存；固件的首次配置、资源更新与恢复出厂均按真机流程执行。

- 功能键支持按住与松开；启动正式 Mini 固件时可能需要长按。
- 右上角提供声音、UART、上下电、复位及实例操作。
- 麦克风在实例设置中启用，首次使用需允许系统麦克风访问；关闭麦克风仍连续播放。
- UART 可在上电前连接，避免遗漏启动日志。
- 宿主网络提供开放逻辑 AP `LISA-Sim`，由固件自行连接并访问服务。

无头工具由同一个 Rust 核心提供实例管理、存储与运行控制，不链接 GPUI，
默认不启用网络、麦克风或扬声器：

```sh
artifacts/desktop/Lisem.app/Contents/MacOS/lisem --help
artifacts/desktop/Lisem.app/Contents/MacOS/lisem create --board arcs-mini --lpk /path/to/firmware.lpk
artifacts/desktop/Lisem.app/Contents/MacOS/lisem run INSTANCE_ID --seconds 30 --timeout 120
```

`run` 在前台执行并返回运行结果，适合 CI；`start` 启动后台实例，后续可用
`stop`、`reset`、`button`、`screenshot` 控制。GUI 和 CLI 使用相同实例库。
CLI 与运行资源一起分发，不应单独复制可执行文件。开发时可用 `make headless`
单独构建 CLI，从仓库目录运行，或通过 `--root` 指定运行资源。

Windows/Linux 包内的 CLI 位于包根目录，分别为 `lisem.exe` 和 `lisem`。

详细操作见[使用说明](docs/usage.md)，实现边界见[架构说明](docs/architecture.md)，
构建与验证见[开发说明](docs/development.md)。

## 支持范围

支持原始 ROM 启动、LPK 导入、串口烧录、双核与 LUNA 推理、ST7789 显示、
连续宿主录播、Wi-Fi 宿主网络，以及逻辑 BLE 对端配网，可运行 Mini 固件的
麦克风唤醒、云端识别与语音回复流程。

当前不支持真实手机 BLE 宿主桥接、摄像头采集、任意板型编辑或运行快照。
RF 校准采用功能模型；CPU 时钟遵循已实现的 HCLK 配置，未模拟硅片完整
缓存／流水线时延。实际交互延迟取决于宿主负载，音频积压和欠载可通过运行
报告查看。

## 源码与数据

`crates/core/` 保存共享存储和运行控制，`crates/cli/` 提供 `lisem` 命令，
`desktop/` 保存 GPUI 界面，`qemu/` 保存芯片与器件，`native/` 保存宿主接口，
`tools/` 保存构建及验证工具，`boards/` 与 `chips/` 保存硬件描述。

固件、设备实例、构建缓存和运行日志不进入 Git。固定芯片 ROM 是随模型
发布的只读资产。LUNA 以预编译静态库提供，不包含实现源码；支持平台、
接口和版本更新方法见 [LUNA 组件](docs/luna.md)。
独立自有源码采用 MIT，QEMU 相关源码保留 GPL，LUNA／ROM 使用单独的
二进制许可。具体范围见 [LICENSE](LICENSE)；第三方许可见 `LICENSES/` 及
相应源码头部。
