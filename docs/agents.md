# AI 接入

在支持本地 MCP 服务的 AI 客户端中连接 Lisem，让 AI 查询实例、操作设备，
并通过 UART、截图等结果辅助调试。本文说明连接与首次使用；直接编写工具
调用时查阅 [MCP 工具参考](mcp.md)，编写脚本时使用 [Python SDK](../python/README.md)。

## 准备 Lisem

先按[快速开始](../README.md#快速开始)安装完整应用包，并启用包内的
`lisem` 命令。在终端运行 `lisem --version`，确认可以启动 CLI。
使用 MCP 不需要先打开图形界面。

选择 AI 要操作的设备实例库。默认与桌面应用使用同一个库；
需要独立的测试设备时，在下面的服务参数中指定新的设备库目录。

## 配置 MCP

在 AI 客户端中添加本地 stdio MCP 服务。使用 `mcpServers` 配置格式的
客户端可参考：

```json
{
  "mcpServers": {
    "lisem": {
      "command": "lisem",
      "args": ["mcp"]
    }
  }
}
```

配置文件位置和添加方式以所用客户端为准。若客户端找不到 `lisem`，
将 `command` 换成包内 CLI 的绝对路径，并保留完整应用目录。
使用独立实例库时，将 `args` 改为
`["--data-dir", "/path/to/test-library", "mcp"]`，替换为实际目录。

保存后按客户端要求重新连接服务，确认 Lisem 工具已出现。
客户端负责启动和关闭服务，无需另开终端长期运行 `lisem mcp`。

## 验证与开始使用

先让 AI 查询可用板型和实例列表，例如：

> 查看 Lisem 支持的板型，并列出已有实例。

连接正常后，可以指定实例和任务，例如查看启动画面、观察串口，或按固件
要求执行长按，再检查响应。新建实例运行应用时，提供目标板型及本机 LPK 路径；
固件包需自行准备。实例列表为空时，可以让 AI 创建测试实例。

同一实例的 GUI、CLI 和 AI 操作会相互影响。关闭 AI 连接会停止该连接自己启动的
运行，不影响只观察过的其他运行；Flash、OTP 和实例设置会保留。
固件需要联网或音频时，在任务中说明所需条件；MCP 启动默认不启用宿主网络
和音频。

## 安装 Lisem skill

MCP 提供操作工具；[Lisem skill](../skills/lisem/SKILL.md) 提供安装、
设备操作、原理图板型配置和固件回归的任务指导。使用支持该 skill 格式的
客户端时，可按其安装方式添加；支持 Skills CLI 的环境可执行：

```sh
npx skills add LISTENAI/Lisem
```

skill 本身不安装 Lisem，也不替代 MCP 服务配置。仅使用命令行的 agent
也可按 skill 指导调用 CLI；需要可重复脚本与 pytest 时，见
[Python SDK](../python/README.md)。
