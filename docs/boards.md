# 自定义板型

板型指定芯片、器件、连线和外部信号。内置 Arcs-Mini、Arcs-Mini3、Arcs-EVB
与 VenusA RD EVB 使用同一格式；实例另存 Flash、OTP、热点、媒体来源及
信号覆盖值。

CLI 和 MCP 可校验或导入：

```sh
lisem board-models
lisem board-validate board.json
lisem board-import board.json
lisem create --board my-board
```

MCP 的 `lisem_catalog` 列出器件端口和参数示例；
`lisem_board_validate`、`lisem_board_import` 分别校验和导入。校验返回可用控件及
装配信息，不修改实例库。导入文件的 `id` 必须与内置板型不同；同 ID 再次
导入会更新使用它的实例，受影响实例须已下电。Flash、OTP 和 UID 不变。
芯片或持久存储布局不兼容的更新会被拒绝。

## 格式

自定义示例见 [Mini 配置](../examples/boards/mini-custom.json)。内置板型定义
位于 `boards/`，其中 [Arcs-Mini3](../boards/arcs-mini3.json) 使用 LS2663 和
EXTIO，包含显示、摄像头、SD、信号输入与 HUSB320-BA USB-C 控制器。

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

- `chip` 选择芯片实现。`ls2684` 与 `ls2663` 共用 ARCS die，PSRAM 分别为
  16 MiB 和 8 MiB；`venusa` 对应 CSK7004J8U。NOR 均为 16 MiB。
- `devices` 的键是器件 ID，`model` 选择内置模型，`parameters` 为该模型参数。
  `label` 仅影响显示。器件不能通过 JSON 定义寄存器或执行脚本。
- `nets` 的键是网络名，`endpoints` 使用 `器件ID.端口`。MCU 焊盘使用
  `chip.PA0`、`chip.PB0` 等名称；`pull` 可为 `up` 或 `down`。
- `links` 连接像素、亮度等类型化数据接口，不替代物理连线。
- `inputs` 声明可在运行中修改的数字量或电压，包括默认值、范围和可选注释。
  电压按分压比例送往 ADC；固件自行采样和解释。

配置只描述板级连接。固件仍须配置控制器和 PinMux，只有实际选中的有效路由
才能传输。导入会拒绝未知模型、端口、参数、重复端点和冲突的固定输出。
MCU 或扩展器 GPIO 之间的直接互连不受支持。

## 器件

| model | 端口和参数 |
| --- | --- |
| `st7789` | `SCK`、`SDI`、`CS`、`DC`、`RESET`、`TE`；`panel_inverted` |
| `display-panel` | `viewport` 含 `x`、`y`、`width`、`height`；`rotation_clockwise` |
| `backlight` | `EN`；`active_level` |
| `gc0328` | `SCL`、`SDA`、`MCLK`、`PCLK`、`HSYNC`、`VSYNC`、`D0`…`D7`；可选 `RESET`、`PWDN`；安装旋转 |
| `cst328` | `SCL`、`SDA`、`INT`；安装旋转 |
| `button` | `OUT`；按下和松开电平 |
| `resistor-keypad` | `OUT`；供电电压、上拉电阻和各按键的对地电阻 |
| `led`、`audio-amplifier`、`signal-probe` | `IN`；有效电平，LED 可指定颜色 |
| `voltage-divider` | `IN`、`OUT`；`numerator` / `denominator` 分压比例 |
| `sd-card-slot` | `CLK`、`CMD`、`DAT0`…`DAT3`；可选 `CD` 和检测有效电平 |
| `listenai-extio-ch32v003-v1.3` | `SCL`、`SDA` 与协议支持的 `PAx`、`PCx`、`PDx` 端口；地址为 `108` 或 `21` |
| `husb320-ba` | `SCL`、`SDA`；可选 `INT_N`、`ID`；地址为 `49` 或 `33` |

ST7789 的 RAM 为 240 × 320，面板的可见窗口及安装旋转独立配置。像素与
背光分别通过 `lcd.pixels → screen.pixels`、
`backlight.brightness → screen.brightness` 连接。宿主图片、摄像头和麦克风
来源在实例中选择，不写进板型。

EXTIO 是固定协议器件，版本号属于模型名；包含 GPIO、ADC 和 PC3 PWM。
保留的 GPIO 中断及未支持的命令返回 NACK。外部信号可连接扩展器端口；
SD 的机械检测也可接到扩展器，独立于 SD 控制器自身的检测输入。

HUSB320-BA 的 `partner` 数据接口可通过 `chip.usb0.host` 接到虚拟 USB
主机。连接和断开由实际主机传输端点触发，对端提供 CC1 默认 Rp 和 5 V VBUS；
器件经过虚拟时钟去抖后更新状态及中断。I²C 寄存器与原固件的 USB 枚举、ADB
传输分别执行。GPIO 模式、附件、Try.SRC、手动强制命令和供电跌落未建模；
不支持的寄存器写入返回 NACK。VenusA 可装配该 I²C 器件，尚无 USB 主机传输接口。

每种显示、摄像头、触摸、SD 插槽、扩展器及 USB-C 控制器各支持一个，
按键最多 16 个。
SPI、I²C、DVP、SD、PWM 的可用焊盘由芯片适配层决定；未支持的路由明确拒绝。
