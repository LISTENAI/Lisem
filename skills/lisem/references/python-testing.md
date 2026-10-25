# Python SDK 与 pytest

用 CLI/MCP 探索输入和观察点，再按需要把可重复的操作与判据写成 SDK 脚本或
pytest。用例设计、夹具、时钟、证据及结果分类沿用
[固件自动回归](firmware-regression.md)，这里仅说明 SDK 接入和文档选择。

## 读取匹配的文档

先核对 `lisem --version`。从 [Lisem 仓库](https://github.com/LISTENAI/Lisem)
或 [Releases](https://github.com/LISTENAI/Lisem/releases) 选择对应源码：正式包
使用同版本 `vX.Y.Z` 标签，候选使用交付清单明确的 commit/ref。同一版本号的
候选也可能不同，不能把构建指纹当作 Git commit。

将以下 URL 的 `<ref>` 替换为该标签或已确认的候选 ref：

```text
https://github.com/LISTENAI/Lisem/blob/<ref>/python/README.md
https://github.com/LISTENAI/Lisem/blob/<ref>/python/docs/README.md
https://github.com/LISTENAI/Lisem/blob/<ref>/python/examples/README.md
```

先读 SDK README 的环境与入口，再从 API 索引按需打开生命周期、输入、观测、
错误和 pytest 页；固件脚本另读示例。API 索引和示例同样使用所选 ref，再沿
相对链接阅读，避免跟随指向另一版本的标签链接。

标签尚未发布或页面不可读时，读取同包 `runtime/python-docs/README.md` 和
`runtime/python-docs/docs/README.md`，示例位于其 `examples/`。
macOS 的这些路径在 `Lisem.app/Contents/Resources/` 下。不要回退 master 或
其它版本猜测 API；候选有已发布源码 ref 时也可按上述路径读取。包未提供所需
SDK 能力或对应文档时，记录具体缺口，按该版本实际能力继续可执行的部分。

## 选择测试环境

Python 要求以匹配版本的 SDK README 为准。SDK 随完整应用包提供，测试依赖
装入用户 venv，不另装 SDK wheel。将示例脚本和用例路径换成测试工程中的文件：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install pytest hypothesis
lisem python --interpreter .venv/bin/python -- test_script.py
lisem python --interpreter .venv/bin/python -- -m pytest tests/test_device.py
```

Windows 使用已安装的 Python 创建环境，venv 解释器路径为
`.venv\Scripts\python.exe`。显式 `--interpreter` 选择测试环境；省略时使用当前
激活的虚拟环境或 PATH 中的解释器。工程有依赖清单时按清单安装测试依赖。
`lisem python --sdk-path` 查询同包 SDK 路径，可能是 zip 文件；IDE、Notebook
及已有启动流程的接入读 API 生命周期页。

## 落实为测试

先按回归指导列出前置状态、操作、观察点和通过判据，再组织 fixture 与断言。
从匹配文档读取所需接口，分别配置虚拟运行预算和宿主期限，不以工具调用成功
代替业务结果。按用例选择既有或临时实例，独立试验优先隔离设备库和可写
介质；保留用户指定的实例及跨轮持久化场景，核对 UID 和服务身份是否相互影响。

用上下文或 `finally` 清理测试拥有的运行与资源；失败先保存结构化状态、异常
及所需原始 UART、画面或音频，清理契约及未知交付处理见错误页，pytest 的
fixture 与失败留存见 pytest 页。[判定与交付](firmware-regression.md#判定与交付)
区分能力不支持、外部阻塞、执行错误和业务失败；视觉、语音与交互体验的 AI
或人工补充观察按回归指导记录依据和覆盖范围。
