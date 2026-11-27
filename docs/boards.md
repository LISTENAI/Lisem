# 自定义板型

板型 JSON 描述一块板上的芯片、器件、连线和外部输入。器件模型定义端口及
行为，板型将它们组合成具体硬件。实例选择板型，并独立保存固件、存储和
运行设置；同一板型可以用于多个实例。

器件模型随源码维护；JSON 不定义新的器件行为。需要新增型号或扩展模型时，
贡献者按[外设开发与接入](peripherals.md)修改源码并验证，再配置板型。

## 导入和使用

Arcs-Mini2 的内置板型 ID 为 `arcs-mini2`；旧 ID `arcs-mini` 是其兼容别名，
可用于已有脚本和实例。自定义板型不能使用内置 ID 或其别名。

为板型设置唯一的 `id` 和便于识别的 `name`，保存为 JSON 文件。在桌面的
「新建实例」中点击「导入板型…」，选择文件，再选择该板型创建实例。

命令行先校验文件，再导入并创建实例。以下命令使用下面示例的 `my-board`：

```sh
lisem board-validate board.json
lisem board-import board.json
lisem create --board my-board
```

校验只检查配置；导入后板型保存在实例库中，创建的实例具有独立的 Flash、
OTP 和设备身份。通过 MCP 管理板型见[工具参考](mcp.md#板型与器件)。
固件文件、宿主摄像头、音频来源、SD 镜像和 Wi-Fi 热点在实例中配置。

## JSON 结构

以下示例选择 LS2684，并将一个低电平按键接到 PB4，用于说明各字段的关系：

```json
{
  "schema_version": 1,
  "id": "my-board",
  "name": "我的板型",
  "chip": "ls2684",
  "devices": {
    "flash0": {
      "model": "spi-nor",
      "parameters": { "capacity_bytes": 8388608, "chip_select": 0 }
    },
    "key": {
      "model": "button",
      "label": "功能键",
      "parameters": { "pressed_level": 0, "released_level": 1 }
    }
  },
  "nets": {
    "KEY": { "endpoints": ["chip.PB4", "key.OUT"], "pull": "up" }
  },
  "links": [{ "from": "chip.flash.data", "to": "flash0.bus" }],
  "inputs": {}
}
```

| 字段 | 用途 |
| --- | --- |
| `schema_version` | 描述格式的版本；本文示例使用 `1` |
| `id`、`name` | 板型 ID 和显示名称 |
| `chip` | 芯片描述的 ID，决定控制器、引脚和存储能力 |
| `devices` | 器件 ID、模型、参数和可选显示名称 |
| `nets` | 网络名、连接端点和可选上下拉 |
| `links` | 器件间的像素、亮度等数据接口 |
| `inputs` | 运行中可调节的数字量、电压和照度输入 |

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

### GPIO 扩展器与功放

每块板型当前最多装配一个 GPIO 扩展器。`gxa574` 使用 I²C 地址 `0x20`
（A0/A1/A2 接低），端口为 `P0`～`P7`：写 0 驱动低电平，写 1 释放并由
弱上拉提供高电平，外部输入可将其拉低。模型支持端口读写、数字输入和事件
采集，不提供 ADC、PWM 或中断输出。EXTIO 的端口与固定协议另由其能力目录
描述。

`aw8737a` 实现 AW8737AQNR 的 SHDN 脉冲选模、锁存、启动和关断时序。
将芯片 Codec 的 DAC 数据接口连接到功放的 `analog` 端口，并在 `nets` 中
连接 `SHDN`。宿主播放按功放实际使能状态门控；PCM 保持归一化，不模拟
模拟增益、压限、升压电路、温度保护或扬声器声学特性。

### 固定功放使能

通用 `audio-amplifier` 可通过 `IN` 接 GPIO，并用 `active_level` 指定有效电平。
若使能脚由板级固定偏置控制，使用 `fixed_enable`，不声明 `IN` 连线或
`active_level`：

```json
"speaker": {"model": "audio-amplifier", "parameters": {"fixed_enable": true}}
```

`false` 表示固定关闭。使能状态控制宿主播放及板级反馈，原始 DAC 样本仍完整保留。
该模型表示理想使能，不模拟具体功放的模拟增益、电源或声学特性。

### NOR 存储

每颗 NOR 使用独立的 `spi-nor` 器件，参数 `capacity_bytes` 声明字节容量，
`chip_select` 声明片选编号，各颗容量可以不同。使用 `links` 将
`chip.flash.data` 连接到每颗器件的 `bus`。CS0 使用控制器专用片选；
其它片选的接线和容量限制通过能力目录查询，固件负责设置控制器与 PinMux。

实例镜像的组织方式见[存储架构](architecture.md#存储与外部连接)，
写入偏移见[写入原始二进制](usage.md#写入原始二进制)。

### VenusA PSRAM 与引脚路由

VenusA 板型可用顶层 `memory.psram_bytes` 指定 Xccela PSRAM 容量，支持
`8388608`（8 MiB）和 `16777216`（16 MiB）。省略或设为 `0` 时使用 8 MiB；
内置 RD EVB 保持 8 MiB。此字段独立于 `spi-nor` 的 Flash 容量，不用于
推断芯片丝印或封装。16 MiB 配置提供独立的高半区及对应容量识别寄存器。

SPI0/SPI1 的普通串行数据与片选、I²C0/I²C1 和 PWM0–PWM7 按芯片
PinMux 表路由。板型只声明物理接线，固件负责写入 Core IOMUX；PB0–PB5
还须在 AON IOMUX 选择 NORMAL，才能连接 Core 信号。GPIOA 和 PB0–PB5
使用 Core 功能 0；PB6–PB21 的 GPIO 路径使用功能 1。PB6–PB21 是模拟器
对 GPIOC、SDIO 和 Flash 复用引脚的 GPIO bank 编号，不是额外封装引脚。

I²C 接线须能唯一确定控制器，支持的组合见 `wiring.i2c`；同一控制器上的
器件共用 SCL/SDA。SPI 的重复输入选择不具有已验证的仲裁行为，明确报错。
I²S0/I²S1 支持各自 Core 功能 9/10 的时钟、帧同步和串行数据路线，
同一信号只能选择一个引脚；运行中改变正在传输的路线明确报错。
路由支持不代表实现了复用表中的全部外设；视频 SPI/QSPI 等模式仍按控制器
实际能力校验。

`resistor-keypad.OUT` 可接 PB0–PB3，分别对应 GPADC VIN0–VIN3。
固件须将对应 AON 引脚设置为功能 4、模拟选择 0，再配置 ADC 的参考电压
和输入分压。模型按实际电阻计算电压，支持同时按键；复位保留外部按键
状态。未接通模拟路径时采样明确报错。

### VenusA 摄像头引脚

GC0328 的 SCCB 接线由 `wiring.venusa.i2c` 查询；MCLK、PCLK、HSYNC、
VSYNC 和数据线由 `wiring.venusa.dvp` 查询。像素及传感器时钟路线须启用
Core Function 13，PB0–PB5
还须接通 AON NORMAL。当前八位像素使用接收器 D4–D11，不能把低八位接线
当作等价路线。RESET 和 PWDN 可不接 MCU，分别保持释放和低电平。

### CST836U 单点触摸

`cst836u` 使用 I²C，`address` 默认为七位地址 `0x15`。`width`、`height`
指定传感器原始坐标范围（每轴 1–4096），`rotation_clockwise` 指定板级安装
方向；坐标变换不写入触摸 IC 寄存器。SCL、SDA 和 INT 须连接，RESET 可省略，
表示固定释放。上电及有效硬件复位后须等待 100 ms。

模型支持正常模式、单点数量与 12 位坐标，读取事务内保持同一份坐标快照。
INT 使用低电平模式：接触时拉低，释放后拉高。事件标志固定为 0，不用于
区分按下、移动和释放；固件应按触点数量判断有效状态。写入深睡后必须通过
硬件复位恢复，RESET 固定释放的板型不能靠 I²C 命令唤醒。

`chip_type` 可选，用于提供已经确认的 16 位型号寄存器值；未配置时身份读取
明确拒绝。固件版本、手势、接近检测、工厂模式、多点及边沿中断模式尚未实现。

### 未接 MCU 的器件信号

可省略的信号以 `board-models` 中的 `signals[].required` 为准。
ST7789 的 `RESET` 不接 MCU 时可省略，表示复位输入固定释放；软件复位
仍由屏幕命令执行。`TE` 可省略，表示屏幕同步输出没有接入 MCU，不能据此
产生 GPIO 中断。SCK、SDI、CS 和 DC 仍须接线。

GC0328 的场景输入为理想 ISP 处理后的 640 × 480 RGB 图像，支持 VGA、
QVGA 和 QQVGA。整数抽样模式 `0x5a=0x03`、全零行列选择表按比例从
原点选取处理后的像素，保留全视场；不模拟内部 Bayer 邻域处理及模拟噪声。
裁剪、镜像、输出格式和接收器 DMA 仍由固件配置。

GC0328 的 `PWDN`、`RESET` 可省略，分别表示固定解除掉电和固定释放复位。
这些约定表达板级固定状态，不模拟上电 RC 波形；需要 GPIO 控制时应声明
真实接线。其他器件的输入不能仅因未接 MCU 就任意省略，须按各自模型契约配置。

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

照度输入通过 `device` 选择支持照度信号的器件，不连接数字网络。例如：

```json
"inputs": {
  "illuminance": {
    "type": "illuminance", "device": "ambient", "unit": "lux",
    "range": {"min": 0, "max": 60000}, "default": 550
  }
}
```

`ambient` 是板型中器件的 ID。输入使用整数 lux，范围须符合该模型的能力目录。

## UART 蜂窝模组

`ml307c-gc-cn` 提供 ML307C-GC-CN 的 AT/socket 子集，器件无 JSON 参数。
`TX`、`RX` 按模组方向命名。LS26 当前支持 UART2 的两组接线：
模组 TX/RX 分别连接 PA8/PA9，或 PB7/PB6。固件须配置相应 AF4 和 UART 时钟。
端口为 8N1，初始 115200 baud，可通过 `AT+IPR` 切至 460800 或 921600。

```json
"devices": {
  "cellular": {"model": "ml307c-gc-cn"}
},
"nets": {
  "MODEM_TX": {"endpoints": ["cellular.TX", "chip.PA8"]},
  "MODEM_RX": {"endpoints": ["cellular.RX", "chip.PA9"]}
}
```

这段配置须与完整板型合并。每根 UART 网络仅连接一个芯片焊盘和模组端口，
不支持与 SD 等器件共线。每个板型最多一个模组；安装后
UART2 由模组占用，不提供宿主串口连接或字节注入。内置板型不默认安装模组。
具体命令、网络与 SIM 限制见 [UART 蜂窝模组](devices/modems.md)。

## 更新已有板型

修改 JSON 后，以相同 `id` 再次导入即可更新板型；使用该板型的实例须先下电，
下次上电应用新配置。更新保留实例的 Flash、OTP 和 UID，并检查芯片和持久
存储布局的兼容性。自定义板型使用独立于内置板型的 ID。

## 参考实现

内置板型位于 `boards/`，可用于查阅具体的器件选择和连接方式：

- [Arcs-Mini2](../boards/arcs-mini2.json)：显示控制器、面板、背光及摄像头的组合。
- [Arcs-Mini3](../boards/arcs-mini3.json)：GPIO 扩展器、USB-C 角色控制器及外设连接。
- [Arcs-EVB](../boards/arcs-evb.json)：触摸面板与电阻按键。
- [VenusA RD EVB](../boards/venusa-rd-evb.json)：GC0328 摄像头、串口、SD 卡及芯片引脚配置。

[自定义 Mini 示例](../examples/boards/mini-custom.json)可作为导入配置的起点。
