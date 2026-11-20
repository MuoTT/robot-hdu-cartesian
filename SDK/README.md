# Robonix XYZ 绘图 SDK

把 PNG 图片或单线文字转成 XYZ 三轴绘图轨迹（毫米），通过 TCP 下发到 PLC
（MatrixBox MX4001），由 PLC 轴组功能块驱动三轴机器人绘图。

## 目录

```
SDK/
├── robonix_sdk/              # SDK 包（几何层与会话层互不依赖）
│   ├── protocol.py           # 二进制帧 + CRC16-Modbus
│   ├── client.py             # RobonixClient（TCP 客户端，逐帧握手）
│   ├── image.py              # PNG 读取 + Otsu 二值化
│   ├── trajectory.py         # 二值图 -> 轮廓轨迹（marching squares + RDP）
│   ├── text.py               # 单线文字排版（Hershey 西文 + 汉字笔画中线）
│   ├── hershey_simplex.py    # Hershey Simplex 字形数据
│   └── hanzi_data.py         # 3755 个常用汉字笔画中线数据
├── tests/                    # 测试与示例脚本
│   ├── draw.py               # 绘图脚本（png / text 子命令）
│   └── plc_framing_test.py   # PLC 分帧 / 协议测试
└── docs/
    ├── protocol.md           # 通信协议（核心文档）
    ├── plc_wiring.md         # PLC 侧实现与接线
    └── text.md               # 文字书写说明
```

依赖：`numpy` + `Pillow`（标准库 `socket` / `struct`）。

## 分层

| 层 | 模块 | 职责 | 是否碰网络 |
|----|------|------|-----------|
| 几何层 | `image` / `trajectory` / `text` | 画布、轨迹、文字排版 | 否 |
| 会话层 | `protocol` / `client` | 帧编解码、TCP 握手、运动命令 | 是 |

坐标与单位全库统一：

- 毫米，原点在纸面左下角，`+X` 向右、`+Y` 向上；
- 机器零点 `(0,0,0)` = 纸面左下角上方 50 mm；
- 纸面 `z = -50`，落笔 `z_down = -50`，抬笔 `z_up = -46`（离纸 4 mm）；
- 机器行程上限：`X 180 mm`、`Y 150 mm`。

接入 Robonix 时：几何层进 skill 包，会话层进 primitive 包；标准机械臂契约用米，
换算 `(mm / 1000.0)` 放在 primitive 边界做，SDK 内部始终保持毫米。

## 快速上手

### 1. 离线预览（无硬件）

```bash
# PNG：默认按 180×150 mm 纸张自动等比居中
python tests/draw.py png --input drawing.png --dry-run

# 保存二值化结果，便于核对阈值
python tests/draw.py png --input drawing.png --dry-run --debug-binary

# 文字：支持大小写英文、数字、汉字、空格、换行
python tests/draw.py text "Hello 你好 123" --dry-run
```

dry-run 会打印自动适配的 `scale/offset`、轨迹点数、实际范围，以及一条
`MOVE_LINEAR` 示例帧（24 B 载荷 + 8 B 帧头/CRC = 32 B）。PNG 模式还会把
轨迹范围与纸张可用区比较：dry-run 越界只告警，实际下发会拒绝发送。

### 2. 连接真实 PLC

```bash
python tests/draw.py png --input drawing.png --host 192.168.33.130 --port 2000
python tests/draw.py text "Hello 你好" --host 192.168.33.130 --port 2000
```

绘制/书写前后都会执行 `HOME`；不需要时加 `--no-home`。

### 3. 测试

```bash
# 协议分帧/粘包/半包/CRC 测试（需要真实 PLC 在线）
python tests/plc_framing_test.py --host 192.168.33.130 --port 2000 --run-all
```

## 常用参数

### PNG 模式

| 参数 | 默认 | 说明 |
|------|------|------|
| `--scale` | 自动 | 像素→毫米比例；省略时调用 `fit_to_paper` 等比居中 |
| `--offset-x` / `--offset-y` | `0.0` | 世界坐标偏移 (mm)，仅在指定 `--scale` 时生效 |
| `--paper-width` / `--paper-height` | `180` / `150` | 纸张/行程范围 (mm)，下发前检查（旧名 `--work-*` 仍可用） |
| `--margin` | `5` | 纸张四周留白 (mm) |
| `--width-mm` / `--height-mm` | 纸张 80% | 自动适配时的绘制框 (mm) |
| `--fill` | `0.8` | 自动适配时占纸张的比例 |
| `--simplify-px` | `1.5` | 轮廓 Douglas-Peucker 抽稀容差（像素），`0` 关闭 |
| `--threshold` | Otsu | 手动二值化阈值（0~255） |
| `--invert` | 关 | 亮像素视为墨迹 |
| `--flip-x` / `--flip-y` | 关 | 坐标轴翻转 |
| `--debug-binary [PATH]` | 关 | 保存二值化图片（省略 PATH 时输出 `<input>_binary.png`） |

### 文字模式

| 参数 | 默认 | 说明 |
|------|------|------|
| `--font-size-mm` | `10.0` | 大写字母 / 汉字墨迹高度 (mm) |
| `--line-spacing` | `1.5` | 行高 = 字号 × 该倍数 |
| `--letter-spacing` | `0.5` | 字间距 (mm) |
| `--space-advance` | 0.6×字号 | 空格宽度 (mm) |
| `--origin-x` | `5.0` | 每行起笔的绝对 X (mm) |
| `--reset-y` | `150.0` | `--reset` 后文字块顶边的绝对 Y (mm) |
| `--state` | `~/.robonix/writer_state.json` | 游标状态文件 |
| `--reset` | 关 | 重置游标到左上角 |
| `--new-line` | 关 | 写完后游标落到下一行行首（默认停在行尾，下次接着写） |

### 公共参数

| 参数 | 默认 | 说明 |
|------|------|------|
| `--z-up` / `--z-down` | `-46` / `-50` | 抬笔 / 落笔高度 (mm) |
| `--vel` | `20.0` | 运动速度 (mm/s)，通过 `SET_PARAM` 下发 |
| `--acc` / `--dec` / `--jerk` | `300` / `300` / `3000` | 运动参数，通过 `SET_PARAM` 下发 |
| `--host` / `--port` | `192.168.33.130` / `2000` | PLC 地址 |
| `--timeout` | `30` | 连接超时 (s)，同时作为逐点 ACK 超时的下限 |
| `--dry-run` | 关 | 只生成轨迹，不连接 PLC |
| `--no-home` | 关 | 绘制前/结束后都不回零 |

> 点数过多时：优先调大 `--simplify-px`（如 `2~5`）；复杂图片也可先降低分辨率，
> 再用显式 `--scale` 保持实际绘制尺寸。

## 编程接口

```python
from robonix_sdk import RobonixClient, PlotConfig, fit_to_paper, load_binary, to_waypoints

binary = load_binary("drawing.png")
scale, ox, oy, w, h = fit_to_paper(binary.shape[1], binary.shape[0])
cfg = PlotConfig(pixel_to_mm=scale, offset_x=ox, offset_y=oy)
points = to_waypoints(binary, cfg)          # 轮廓细描（唯一策略）

with RobonixClient("192.168.33.130") as plc:
    plc.set_param(15.0, 300.0, 300.0, 3000.0, -46.0, -50.0)  # 速度/加减速
    plc.home()
    plc.draw_waypoints(points)
    plc.home()
```

文字模块不在包根导出，按子模块导入：

```python
from robonix_sdk.text import TextConfig, TextWriter

writer = TextWriter(TextConfig(font_size_mm=10.0))
with RobonixClient("192.168.33.130") as plc:
    writer.write(plc, "Hello 你好", home=True, next_line=False)  # 内部自动 SET_PARAM
```

## 协议与 PLC 现状

SDK 只暴露当前 PLC 已实现的命令：

- `PING(0x00)`、`MOVE_LINEAR(0x01)`、`HOME(0x03)`、`SET_PARAM(0x04)`、`CLOSE(0x0A)`；
- `MOVE_LINEAR` 载荷为 `x, y, z = 3×f64`（24 B），速度由之前的
  `SET_PARAM` 决定；
- `SET_PARAM` 载荷为 `vel, acc, dec, jerk, z_up, z_down = 6×f32`（24 B），
  与 PLC 的 `REAL` 布局一致；参数在 PLC 侧全局生效。

细节见 `docs/protocol.md`，PLC 侧实现见 `docs/plc_wiring.md`。