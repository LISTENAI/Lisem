# 构建与维护

## 入口

- `make build`：构建并封装自包含的 release 应用、CLI 和原生运行组件。
- `make run`：打开已构建的应用。
- `make headless`：构建不依赖 GPUI 的 Rust CLI。
- `make test-core`：Rust 共享核心与 CLI 测试。
- `make test`：Python 验证工具与 Rust workspace 测试。
- `make check-qemu`：CPU、时钟、器件、存储、控制和宿主传输回归。

单项保留 `make qemu-audio`、`make qemu-display`、`make qemu-uart` 等入口。
运行 CPU 探针需要 RISC-V bare-metal GCC，`CROSS_COMPILE` 指定工具链前缀。
应用固件只作为外部测试输入，不提交其源码、微程序或实例副本。

QEMU 和音频构建记录输入/产物哈希，启动前拒绝过期模型。上游源码和下载
缓存放 `.tools/`，生成物放 `artifacts/`。不得在生成目录修改源码后交付。

无归档实时通道可用原始 LPK 验证：

```sh
python3 tests/run_live_memory.py --lpk firmware.lpk --audio --microphone --network
```

该测试使用独立临时实例，检查运行中没有自动录制文件、画面和 UART 可用、
音频完整交付、退出清理及 OTP 保持；麦克风与网络选项需要宿主相应能力。

MCP 控制与观察使用 `python3 tests/run_mcp.py`；加 `--lpk firmware.lpk`
验证原应用启动。GHA 对每个平台的最终包运行不依赖应用固件的 MCP 回归。

## 平台工具链

共同依赖 Rust 1.95、Python 3.10+、C 编译器、Ninja、Meson、pkg-config、
Git 和 patch。Python 仅参与构建与验证，不随运行产品启动。

macOS 使用 Xcode Command Line Tools，以及 GLib、Pixman 和 libslirp 的构建依赖。
Linux 使用 Clang/GCC、GLib、Pixman、PulseAudio 开发包、patchelf，以及 GPUI
需要的 Vulkan、Wayland/X11、xkbcommon、fontconfig、OpenSSL、DBus 和 libsecret
开发包。Ubuntu 24.04 的桌面音频可通过 PipeWire 的 PulseAudio 兼容服务连接。
纯文本 Linux 的 CLI 不需要显示服务器；未开启宿主音频时也不需要音频服务。

Windows 使用原生 MSVC Rust 工具链和对应架构的 Visual Studio C++ Build Tools、
Windows SDK；QEMU、音频和网络组件使用 MSYS2 的 Clang、GLib、Pixman、
PortAudio、Python、Meson、Ninja、pkgconf、Git、patch 和 GNU tar。
ARM64 使用 CLANGARM64 环境，x64 使用 UCRT64 环境。先进入对应架构的
Visual Studio Developer Command Prompt，再将 MSYS2 工具目录加入 PATH，
设置 `CC=clang`，执行 `python tools/desktop.py --build-only`。
Rust 的链接器仍由 Visual Studio 提供。构建架构必须与依赖库架构一致。

Windows 和 Linux 的产物为 `artifacts/desktop/Lisem/`，分别运行
`lisem-desktop.exe` / `lisem-desktop` 或 `lisem.exe` / `lisem`。
整个目录一起分发，不能只复制一个可执行文件。打包递归收集非系统动态库、
许可和资源，并校验依赖闭包与私人路径。Linux 保留系统 glibc 和图形驱动依赖，
应在声明支持的最旧发行版上构建；不能将较新 glibc 构建当作通用 Linux 二进制。
Windows VC 运行库随应用本地部署，不依赖开发机安装目录。

修改 Windows/Linux 音频端点时运行
`python tests/run_host_audio_endpoint.py`，并验证原固件连续音频、显示及网络。
当前虚拟机验证覆盖 Windows 11 ARM64 和 Ubuntu 24.04 ARM64；其它架构和
发行版必须使用对应系统原生构建并验证，不能以交叉编译成功替代运行验收。

## 持续集成与产物

`.github/workflows/build.yml` 在推送、Pull Request 和手动触发时，对 macOS、
Windows、Ubuntu 24.04 的 ARM64/x86_64 进行原生构建。`tools/ci.py` 运行
Rust/Python 测试、适用的平台端点测试，并在移出的应用包中验证无头 CLI、
空白实例、原始 ROM UART 握手及存储不变。Linux x86_64 另运行完整芯片回归。
这些检查不需要应用固件、平台凭据、图形会话或真实音频设备。

每个平台的原生组件、Rust release 构建和 debug 测试在独立 job 中并行运行。
debug job 运行共享核心和 CLI 的测试；桌面入口没有单元测试，使用 release
构建和打包后的生命周期集成测试验证，不重复编译 GPUI 的 debug 依赖。
打包只等待本平台的两份构建产物，校验提交、平台和文件哈希后执行包集成测试，
上传候选产品包、SHA-256 与构建提交信息。Rust 依赖按工具链、平台和 profile
缓存；原生代码使用 ccache，仍执行构建与全部回归。Windows 复用已安装的
MSYS2 工具链，按架构、依赖配置和月份隔离；打包与原生构建使用同一份依赖。
阶段耗时记录在 job summary。

artifact 上传不代表发布验收通过；任何构建、测试或打包失败、取消或跳过时，
总检查 `All checks passed` 均不能成功。发布必须使用整次工作流成功的产物。
新增自动发布 job 应依赖总检查；独立发布工作流还须核对源 run 的成功状态和提交。

macOS/Linux 使用保留执行权限的 tar.gz，Windows 使用 zip。正式发布采用
通过 CI 的归档，不用本地构建替换。LUNA 与 ROM 直接使用仓库随附字节。
原始固件业务和声学效果仍按下面的步骤独立验收，不能由 CI 握手测试替代。

发布时使用版本标签对应的成功 CI 产物，将六个平台的归档和各自的 SHA-256
文件附到同一 GitHub Release。核对包内版本、标签和构建提交一致；macOS
同时核对包内 `LSMinimumSystemVersion`，按实际依赖要求声明支持的系统版本。

macOS 的 Homebrew 分发使用 [LISTENAI 公共 tap](https://github.com/LISTENAI/homebrew-tap)
中的 `lisem` cask。cask 的 `app` 安装 `Lisem.app`，`binary` 链接包内
`Contents/MacOS/lisem`；安装和升级共用同一应用包。首次接入需在 tap 添加
`Casks/lisem.rb` 及 `projects.yaml` 的 `cask` 项，资产分别匹配
`Lisem-darwin-aarch64.tar.gz` 和 `Lisem-darwin-x86_64.tar.gz`。初始校验值
取自正式 Release，后续版本由 tap 的现有更新流程维护。cask 普通卸载保留
实例数据，不在升级时终止或删除设备实例。

## 原固件验证

使用实际发布 LPK；与独立指令探针和测试图分别记录结果。以下测试创建
独立实例，不使用日常设备库。桌面测试默认使用 `make build` 生成的
平台对应的 CLI，可用 `--binary` 指定其它构建：

```sh
python3 tests/run_qemu_lpk_boot.py --lpk /path/to/firmware.lpk --output artifacts/verify-boot
python3 tests/run_qemu_rom_firmware.py --flash-image /path/to/flash.bin --output artifacts/verify-rom
python3 tests/run_desktop_uart_firmware.py --lpk /path/to/firmware.lpk --output artifacts/verify-uart
python3 tests/run_desktop_network_firmware.py --lpk /path/to/firmware.lpk --output artifacts/verify-network
python3 tests/run_desktop_cskburn.py --cskburn /path/to/cskburn --lpk /path/to/firmware.lpk --output artifacts/verify-burn
```

修改相关链路须验证 ROM 身份、完整 LPK 烧录/回读、Flash 持久化、UART
预连接/复位/上下电和实例隔离。错误必须来自原固件或模型的可观测状态，
不能把进程存活或计时完成当作业务成功。

已配置实例可用以下入口创建受锁保护的独立副本，验证显示、连续音频和网络：

```sh
python3 tests/run_desktop_media.py --instance /path/to/device --output artifacts/verify-media --microphone --seconds 60
```

默认宿主静音；`--sound` 启用实际播放。`--input /path/to/voice.wav` 可替代
麦克风进行可重复的语音测试。声学 AEC 验收需要实际播放及麦克风，不能用
文件输入或静音运行替代。

原固件语音验收同时开启画面、连续录播和网络，检查麦克风唤醒、云端识别、
回复播放、ADC 缺样、宿主欠载及队列积压。原始 DAC 不裁剪静音；与实际
网络回复独立解码对照。宿主队列消费不等于声学播放验证。

## 数值与性能

LUNA 二进制组件的更新与验收见 [LUNA 接口](luna.md)。公开测试验证资产完整性
和接入行为；原固件推理、连续音频和网络用于产品链路验收。

`tools/qemu_benchmark.py` 以相同输入按 ABBA 串行比较运行时，并核对完整
报告、指令数、画面、WAV、UART 与报文输出。测量期间不并行构建或跑回归。
吞吐、时钟模型和真实交互延迟分别评估，不将单个局部加速等同于整机实时。

## 发布边界

`Lisem.app` 内 `Contents/MacOS` 保存 GUI 和 CLI，`Resources/runtime` 保存
QEMU、音频、网络与硬件描述，`Frameworks` 保存递归收集的非系统动态库。
库引用使用相对装载路径；构建路径从调试信息及诊断位置中重映射。打包检查
依赖闭包、私人路径、签名及组件要求的最低 macOS 版本，随包保留第三方许可。
Apple Silicon 默认以 Apple M1 指令集构建 QEMU，不采用构建机器专用指令。

GUI/CLI 从可执行文件位置发现运行资源；启动时校验包内资产及动态库哈希。
运行不读取源码构建记录。实例存储写入设备库，应用包保持只读。正常运行的音视频和 UART
不自动写盘；需要诊断归档时使用 `lisem run ID --capture DIRECTORY`。
`--root` 和 `LISEM_ROOT` 可显式指定开发运行资源，不写入构建机器绝对路径。

发布前将包搬到仓库外，禁止子进程访问源码、Homebrew 与 Cargo 目录，验证
原固件启动、UART、画面、音频和网络：

```sh
python3 tests/run_macos_bundle.py --lpk /path/to/firmware.lpk --output /tmp/lisem-bundle-check
```

macOS 应用使用 ad-hoc 签名，未公证。首次打开可能需要在系统“隐私与安全性”
设置中允许。发布前在声明支持的最低系统版本上完成安装和升级验证。封装
依赖闭包不能替代跨机器兼容性验证，构建机上的第三方库可能提高最终应用的
最低系统版本。
不随应用分发业务固件、用户实例或平台凭据。ARCS 固定 ROM 属于已授权
芯片资产，其哈希与用途见 `qemu/roms/arcs/manifest.json`。

## 应用图标

图标源文件位于 `desktop/assets/app/`，小尺寸使用单独的 `icon-small.svg`。
安装 librsvg 后运行 `python3 tools/update_icons.py` 更新 PNG、ICO 与 ICNS，
一并提交源文件和生成资源；常规构建无需图标转换工具。

macOS 在应用资源中声明 ICNS，包含标准与 Retina 尺寸；Windows 在桌面 EXE
嵌入多尺寸 ICO；Linux 使用应用 ID `com.listenai.emulator`、同名 desktop
入口及 hicolor 图标。修改后验证小尺寸、浅色／深色背景和各系统应用菜单。

设计参考：[Apple 应用图标](https://developer.apple.com/design/human-interface-guidelines/app-icons)、
[Windows 应用图标](https://learn.microsoft.com/en-us/windows/apps/design/iconography/app-icon-design)、
[freedesktop 图标规范](https://specifications.freedesktop.org/icon-theme-spec/latest/)。
