# Lisem Python SDK

用 Python 脚本和 pytest 控制 Lisem，组织固件输入、运行观察和测试资源清理。
SDK 随 Lisem 应用包交付，版本跟随产品；固件启动流程与业务判据由测试工程维护。

## 安装与启动

从 [GitHub Release](https://github.com/LISTENAI/Lisem/releases) 安装对应平台的
Lisem，并启用包内 `lisem` 命令。准备 Python 3.10+ 后即可运行：

```sh
lisem python -- test_script.py
lisem python --interpreter .venv/bin/python -- -m pytest tests/
```

`--` 后的参数交给 Python。解释器依次选择 `--interpreter`、当前
`VIRTUAL_ENV` / `CONDA_PREFIX`、PATH 中的 Python；Windows 虚拟环境的显式
路径为 `.venv\Scripts\python.exe`。pytest 和 Hypothesis 等依赖由测试虚拟
环境提供，可用该环境的 `python -m pip install pytest hypothesis` 安装。
SDK 和原生 CLI 始终来自同一应用包；Lisem 不安装 Python 或更改用户虚拟环境。

## 快速开始

将以下代码保存为 `test_script.py`，用上述命令运行：

```python
from lisem import Client, source_info

print(source_info())
with Client(data_dir="test-output/library") as client:
    with client.temporary_instance(board="arcs-mini") as device:
        with device.start(max_virtual_seconds=10, timeout=30, download=True) as run:
            print(run.status())
```

此例启动内置 ROM，退出时结束自有运行并清理临时设备。测试应用固件时传入
`lpk=`，按需指定 `uid=` 和独立的 `evidence_dir=`。

## API、IDE 与 CI

参数、返回值与所有权见
[API 总览](https://github.com/LISTENAI/Lisem/blob/v1.1.0/python/docs/README.md)，
固件脚本与状态机见
[测试示例](https://github.com/LISTENAI/Lisem/blob/v1.1.0/python/examples/README.md)。
文档和示例也位于应用的 `runtime/python-docs/`（macOS 在 Contents/Resources 下）。

`lisem python --sdk-path` 查询随包 SDK zip 路径；IDE、Notebook 和已有启动流程
的接入方式见 API 生命周期页。`lisem --json python --sdk-path` 给出 SDK 来源及
配套原生构建信息。CI 固定原生归档版本、架构和哈希，再指定测试 venv 解释器。

## 运行条件

启动器保留工作目录、Python 参数、标准输入输出和退出状态。`-E`、`-I`、`-S`
会禁用同包初始化，启动器明确拒绝；自定义隔离可使用查询所得路径自行接入。
临时实例及日记回收需要 POSIX 文件锁。Windows 支持启动与导入，暂不支持
临时实例及日记回收。
