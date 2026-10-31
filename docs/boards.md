# 自定义板型

板型 JSON 描述一块板上的芯片、器件、连线和外部输入。器件模型定义端口及
行为，板型将它们组合成具体硬件。实例选择板型，并独立保存固件、存储和
运行设置；同一板型可以用于多个实例。

## 导入和使用

为板型设置唯一的 `id` 和便于识别的 `name`，保存为 JSON 文件。在桌面的
「新建实例」中点击「导入板型…」，选择文件，再选择该板型创建实例。

命令行先校验文件，再导入并创建实例。以下命令使用下面示例的 `my-board`：

```sh
lisem board-validate board.json
lisem board-import board.json
lisem create --board my-board
```

校验只检查配置；导入后板型保存在实例库中，创建的实例具有独立的 Flash、
OTP 和设备身份。通过 MCP 管理板型见[agent 接入](agents.md#板型与器件)。
固件文件、宿主摄像头、音频来源、SD 镜像和 Wi-Fi 热点在实例中配置。

## JSON 结构

以下示例选择 LS2684，并将一个低电平按键接到 PB4，用于说明各字段的关系：

```json
{
  "schema_version": 1,
  "id": "my-board",
  "name": "我的板型",
  "chip": "ls2684",
  "storage": { "flash_bytes": 16777216 },
  "devices": {
    "key": {
      "model": "button",
      "label": "功能键",
      "parameters": { "pressed_level": 0, "released_level": 1 }
    }
  },
  "nets": {
    "KEY": { "endpoints": ["chip.PB4", "key.OUT"], "pull": "up" }
  },
  "links": [],
  "inputs": {}
}
```

| 字段 | 用途 |
| --- | --- |
| `schema_version` | 描述格式的版本；本文示例使用 `1` |
| `id`、`name` | 板型 ID 和显示名称 |
| `chip` | 芯片描述的 ID，决定控制器、引脚和存储能力 |
| `storage.flash_bytes` | Flash 容量，单位为字节，与所选芯片的存储配置匹配 |
| `devices` | 器件 ID、模型、参数和可选显示名称 |
| `nets` | 网络名、连接端点和可选上下拉 |
| `links` | 器件间的像素、亮度等数据接口 |
| `inputs` | 运行中可调节的数字量和电压输入 |

### 器件与连线

`devices` 中的键是器件 ID；`model` 指定内置模型，`parameters` 提供参数，
`label` 设置界面名称。用当前安装的程序查询器件能力：

```sh
lisem board-models
lisem board-models --category camera
lisem board-models --model GC0328
lisem board-models --category display --details
lisem board-models --schema
```

不带选项时返回完整模型数组。筛选查询返回 `categories`、`query`、`found`、
`models` 和程序构建身份。`category` 使用返回的类别 ID，分类列表默认只含
型号和简介；`--details` 展开详情。`--model` 按型号精确匹配，忽略大小写，
自动返回详情；两种筛选可同时使用。未找到匹配时返回 `found: false` 和空
`models`，未知类别报错。

详情中的 `signals` 描述器件端口的方向、类型及是否必需，方向以器件为参照。
`data_signals` 描述 `links` 使用的数据端口；`parameter_schema` 提供参数类型、
默认值、范围及枚举，`parameter_example` 给出示例。`interfaces` 和 `constraints`
补充总线地址、组合要求和接线条件。模型可装配不等于覆盖实物的全部工作模式，
功能验证仍需匹配固件所用的行为。

`--schema` 另附 `board_schema`、`chips`、`wiring` 和 `boards`。
`wiring` 提供芯片引脚、I²C、ADC 和 SD 路由；`boards` 中的 `definition` 是
可用于编辑的参考板型 JSON。JSON Schema 检查结构与参数，完整接线和跨参数
条件由 `board-validate` 校验。

`nets` 的键是网络名。`endpoints` 采用 `器件ID.端口`，例如 `key.OUT`；
芯片引脚以 `chip.PA0`、`chip.PB4` 等名称表示，扩展器引脚可写为 `extio.PC2`。
网络需要上拉或下拉时，将 `pull` 设为 `up` 或 `down`。

更换接线时，修改对应网络的芯片端点，并使固件的控制器和 PinMux 配置与之
匹配。可用引脚及路由取决于所选芯片。

### 数据接口与屏幕示例

`links` 使用 `from`、`to` 连接器件的数据端口，端口的类型由模型定义。
例如 Mini 的显示部分组合了 `st7789`、`display-panel` 和 `backlight`：
ST7789 的显示 RAM 为 240 × 320，面板的 `viewport` 指定可见区域的
`x`、`y`、`width`、`height`，`rotation_clockwise` 指定安装角度，可取
`0`、`90`、`180`、`270`。

`nets` 描述控制器和背光的接线，`links` 指定面板接收的像素和亮度。例如：

```json
[
  { "from": "lcd.pixels", "to": "screen.pixels" },
  { "from": "backlight.brightness", "to": "screen.brightness" }
]
```

这里的 `lcd`、`screen` 和 `backlight` 对应 `devices` 中的器件 ID。
面板的安装方向通过 `rotation_clockwise` 参数配置。

### 信号输入

`inputs` 将网络暴露为可操作的输入。数字量使用布尔值；电压使用整数 mV，
并声明范围和默认值。下面的输入连接到名为 `USB_DET` 的网络：

```json
{
  "usb_det": {
    "type": "digital",
    "net": "USB_DET",
    "description": "USB 检测",
    "default": true
  }
}
```

`description` 是可选注释。电压输入使用 `type: "voltage"`、`unit: "mV"`
及 `range` 声明取值范围。例如电池电压可取 0–4500 mV，默认 4200 mV。
通过 `voltage-divider` 连接 ADC 时，`numerator` 和 `denominator` 决定分压
比例。完整示例见
[自定义 Mini](../examples/boards/mini-custom.json)的 `inputs` 与电池分压接线。
操作方法见[信号输入](usage.md#信号输入)。

## 更新已有板型

修改 JSON 后，以相同 `id` 再次导入即可更新板型；使用该板型的实例须先下电，
下次上电应用新配置。更新保留实例的 Flash、OTP 和 UID，并检查芯片和持久
存储布局的兼容性。自定义板型使用独立于内置板型的 ID。

## 参考实现

内置板型位于 `boards/`，可用于查阅具体的器件选择和连接方式：

- [Arcs-Mini](../boards/arcs-mini.json)：显示控制器、面板、背光及摄像头的组合。
- [Arcs-Mini3](../boards/arcs-mini3.json)：GPIO 扩展器、USB-C 角色控制器及外设连接。
- [Arcs-EVB](../boards/arcs-evb.json)：触摸面板与电阻按键。
- [VenusA RD EVB](../boards/venusa-rd-evb.json)：GC0328 摄像头、串口、SD 卡及芯片引脚配置。

[自定义 Mini 示例](../examples/boards/mini-custom.json)可作为导入配置的起点。
