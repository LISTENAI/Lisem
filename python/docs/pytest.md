# pytest 集成

[API 总览](README.md) · [错误与清理](errors.md) · [固件测试示例](../examples/README.md)

用测试虚拟环境安装 pytest 和 Hypothesis，然后通过应用启动：

```sh
.venv/bin/python -m pip install pytest hypothesis
lisem python --interpreter .venv/bin/python -- -m pytest tests/
```

Windows 使用 `.venv\Scripts\python.exe`。SDK 的标准 dist-info 与插件入口
随应用提供，pytest 自动发现 `lisem_client`；工程若关闭插件自动发现，可显式
使用 `-p lisem.pytest_plugin`。依赖来自所选 venv，启动器不执行依赖安装。

| 选项 | 用途 |
| --- | --- |
| `--lisem-executable PATH` | fixture 使用的包内 CLI，省略时沿用 Client 查找规则 |
| `--lisem-data-dir PATH` | 显式设备库；默认每测试使用 `tmp_path / "library"` |

`lisem_client` 是每测试创建、teardown 关闭的 Client。测试自己选择板型、
固件及 UID；共用库时协调身份和介质，并行任务使用独立资源。

```python
import pytest


@pytest.fixture
def device(lisem_client, tmp_path):
    with lisem_client.temporary_instance(
        board="arcs-mini", evidence_dir=tmp_path / "evidence",
    ) as instance:
        yield instance


def test_rom_start(device):
    with device.start(max_virtual_seconds=10, timeout=30, download=True) as run:
        assert run.status()["finished"] is False
```

运行：`lisem python -- -m pytest tests/`。随包 Client 默认绑定配套 CLI。
固件测试在 fixture 传入 LPK，通过工程辅助函数准备业务状态，再断言实际
日志、画面、音频或信号。

报告钩子区分 setup、call、teardown 失败；yield fixture 退出时保存证据，
随后产生的 teardown 失败也会更新证据索引。原断言和清理错误分别保留，停止
无法确认时保留资源。固件命令、OTP 布局、业务判据及状态机保留在完整示例和
测试工程，素材与外部服务也由测试工程准备。
