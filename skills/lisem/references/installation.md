# 安装与连接

## 检查已有安装

已有 Lisem MCP 连接时，调用 `lisem_catalog` 确认连接及可用板型。
使用 CLI 时先检查命令位置：macOS/Linux 使用 `command -v lisem`，
Windows 使用 `Get-Command lisem -ErrorAction SilentlyContinue`。
找到后运行 `lisem --version` 和 `lisem --help`。

命令不在 PATH 时，检查已安装或解压的应用包。CLI 随完整应用包提供：

| 平台 | 包内 CLI | 图形界面 |
| --- | --- | --- |
| macOS | `Lisem.app/Contents/MacOS/lisem` | `Lisem.app` |
| Windows | `lisem.exe` | `lisem-desktop.exe` |
| Linux | `lisem` | `lisem-desktop` |

可直接使用 CLI 的绝对路径，无需为自动化操作打开 GUI。
保留完整应用目录，CLI 通过包内相对路径查找 QEMU、原生组件和资源。

## 安装正式包

根据宿主操作系统与 CPU 架构选择正式包，核对对应 Release 的系统要求。
用户指定版本时使用该版本，否则使用最新稳定版。

macOS 已有 Homebrew 时，可安装最新稳定版应用及 CLI：

```sh
brew install --cask listenai/tap/lisem
```

其他情况从 [GitHub Releases](https://github.com/LISTENAI/Lisem/releases)
下载对应附件及其 `.sha256` 文件，核对 SHA-256 后解压到安装目录。
有 GitHub CLI 时可用 `gh release view --repo LISTENAI/Lisem` 查看附件，
再用 `gh release download` 下载选定版本的文件。

附件名为 `Lisem-<system>-<arch>.tar.gz`（macOS/Linux）或 `.zip`（Windows）。
`system` 为 `darwin`、`linux`、`windows`；`arch` 为 `aarch64`（ARM64）或
`x86_64`。macOS 可将 `Lisem.app` 放到应用目录；Linux 解压时保留执行权限。
系统安装目录不可写时，可使用用户可写目录并以绝对路径调用。

## 验证与接入

用实际安装位置运行 `lisem --version`、`lisem --help` 和 `lisem --json list`，
确认 CLI 能读取实例库。实例列表为空是正常结果。CLI 无头运行不需要
显示服务器；未启用宿主音频时也不需要声音服务。

需要通过命令名调用时，macOS 可使用 Lisem 菜单中的「安装命令行工具」，
Windows/Linux 可将完整应用所在目录加入 PATH；仅当前任务使用时直接保留
CLI 绝对路径即可。

需要 MCP 时，在当前 agent 客户端的 MCP 配置中添加 stdio 服务：`command`
使用 CLI 的绝对路径，`args` 为 `["mcp"]`。需要独立实例库时，使用
`["--data-dir", "/path/to/device-library", "mcp"]`，替换为实际目录。
连接后调用 `lisem_catalog` 验证，再按返回的工具能力继续任务。
CLI 与 MCP 均可控制实例，按当前客户端支持的方式选择即可。
