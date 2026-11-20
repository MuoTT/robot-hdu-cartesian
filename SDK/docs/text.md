# 单线文字书写（Hershey 西文 + 汉字笔画中线）

SDK 的 `robonix_sdk.text` 把文字排版成绝对坐标轨迹（毫米），再由
`RobonixClient` 逐点下发到 PLC。模块本身**只排版、不碰网络**。

## 1. 坐标与纸张

| 项 | 约定 |
|---|---|
| 工作范围 | X `0 ~ 180 mm`，Y `0 ~ 150 mm`（机器行程上限） |
| 原点 | 纸张左下角 |
| 方向 | `+X` 向右，`+Y` 向上 |
| 机器零点 | `(0, 0, 0)` = 写字区左下角上方 50 mm（笔尖待机位） |
| 纸面 | `z = -50` |
| 落笔 / 抬笔 | `z_down = -50` / `z_up = -46`（离纸 4 mm） |
| 默认起点 | 文字块左上角，`origin_x = 5 mm`，块顶边 `reset_y = 150 mm` |
| 默认字号 | 10 mm（大写字母 / 汉字墨迹高度） |

首行基线：

```
baseline_y = reset_y - (22 - TOP_UNITS + TOP_SAFETY_UNITS) * scale
           = 150 - (22 - 0 + 1) * font_size / 21
```

所以 10 mm 字号的首行基线约在 `y = 139.05`，字顶正好贴住 `y = 150`。

## 2. 字形与度量

| 来源 | 内容 | 说明 |
|------|------|------|
| `hershey_simplex.py` | `A-Z`、`a-z`、`0-9` | Hershey Simplex (futural)，单线笔画 |
| `hanzi_data.py` | 3755 个常用汉字 | GB2312 一级字，笔画中线（medians） |
| 特殊处理 | 空格、`\n` | 空格按 `space_advance` 前进；`\n` 强制换行 |

- 两套字形共用同一条基线；汉字按字库最高笔画 `Y_MAX = 874` 等比缩放，
  使**汉字墨迹高度 = 字号**，与西文大写字母等高。
- 汉字字格宽 `= 1024 * (21 / 874) * scale ≈ 1.172 × 字号`；
  汉字字距固定为 `CJK_TRACKING_MM = 1.0 mm`。
- 西文字距 = `advance × scale + letter_spacing_mm`；空格宽度默认 `0.6 × 字号`。
- 暂不支持标点符号（`!?.,;:` 等）；字库外的汉字或字符会在排版前抛 `ValueError`。

## 3. 快速使用

```bash
# 预览轨迹，不连接 PLC
python tests/draw.py text "Hello 你好 123" --dry-run

# 写到 PLC（写完自动 HOME）
python tests/draw.py text "Hello 你好" --host 192.168.1.135 --port 2000

# 调整字号、起笔位置
python tests/draw.py text "ABC" --font-size-mm 15 --origin-x 10

# 重置到左上角，并让下次从下一行行首开始
python tests/draw.py text "第一行" --reset --new-line

# 指定状态文件
python tests/draw.py text "ABC" --state ./writer_state.json
```

## 4. 参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `text` | 必填 | 要写的文字（大小写英文、数字、汉字、空格、换行） |
| `--font-size-mm` | 10.0 | 大写字母 / 汉字墨迹高度（mm） |
| `--line-spacing` | 1.5 | 行高 = 字号 × 该倍数 |
| `--letter-spacing` | 0.5 | 西文字间距（mm） |
| `--space-advance` | 0.6×字号 | 空格宽度（mm） |
| `--origin-x` | 5.0 | 每行起笔的绝对 X（mm） |
| `--reset-y` | 150.0 | `--reset` 后文字块顶边的绝对 Y（mm） |
| `--paper-width` / `--paper-height` | 180 / 150 | 纸张范围（mm），旧名 `--work-*` 仍可用 |
| `--margin` | 0 | 四周留白（mm） |
| `--z-up` / `--z-down` | -46 / -50 | 抬笔 / 落笔 Z |
| `--vel` | 20 | 书写速度 (mm/s)，通过 `SET_PARAM` 下发 |
| `--acc` / `--dec` / `--jerk` | 300 / 300 / 3000 | 运动参数，通过 `SET_PARAM` 下发 |
| `--state` | `~/.robonix/writer_state.json` | 游标状态文件 |
| `--reset` | 关 | 重置游标到左上角 |
| `--new-line` | 关 | 写完后游标落到下一行行首 |
| `--dry-run` | 关 | 只排版，不连 PLC |
| `--no-home` | 关 | 写完后不回零 |

## 5. 排版、换行与续写

- 一次 `layout_text` 内，每行都从 `origin_x` 起笔；换行只把基线减去一个行高。
- 西文按空格切成单词整体断行；汉字逐字断行，任意字之间都可以换行。
- 越界、放不下、字号过大都在**发送 TCP 之前**抛 `TextOverflowError`，
  不会下发半截轨迹。
- 游标 `(x_mm, y_mm)` 是**基线坐标**，每次成功写完后保存到状态文件；
  下一次运行从上次结束的位置继续。
- 默认游标停在本次文字的**行尾**，下次接着同一行写；`--new-line` 时落到
  **下一行行首**。
- 状态文件里的游标如果高于当前配置的首行基线（例如从旧的 280×160 纸张
  迁移过来），`load_cursor` 会判定为无效并回到默认起点，避免首行顶出纸张。

## 6. 与 PLC 的配合

- PLC 侧使用 **MCS 绝对运动**，SDK 直接发送绝对坐标；写之前建议先 `HOME`，
  保证轴组有有效参考位置。
- 正式书写必须保证 PLC 侧 `MotionSim = FALSE`，否则只会回 ACK、不会真正运动。
- `TextWriter.write` 会先下发 `SET_PARAM`（速度/加减速/笔高，6×f32），
  再由 SDK 逐点下发 `MOVE_LINEAR`；`z_up/z_down` 已编进每个坐标，
  对 PLC 的运动没有额外影响。
- 写完最后一个笔画后会先抬笔，再执行 `HOME`（`--no-home` 可跳过）。
- 单点 `MOVE_LINEAR` 帧长 32 B（24 B 载荷 + 8 B 帧头/CRC），远小于 PLC 的
  512 B 载荷上限。
- 文字按点逐条下发，受逐点 ACK 限制，较长文本需要一定时间；
  长文本建议先用 `--dry-run` 看点数。

## 7. 编程接口

文字模块不在包根导出，按子模块导入：

```python
from robonix_sdk import RobonixClient
from robonix_sdk.text import TextConfig, TextWriter

cfg = TextConfig(font_size_mm=10.0, origin_x_mm=5.0, z_up=-46.0, z_down=-50.0)
writer = TextWriter(cfg)                 # 自动读取上次游标

with RobonixClient("192.168.1.135") as plc:
    writer.write(plc, "Hello 你好", home=True, next_line=False)
```

只排版不发送：

```python
points, new_cursor = writer.plan("Hello 你好")
print(len(points), new_cursor)
```

直接调用排版函数：

```python
from robonix_sdk.text import TextConfig, layout_text, text_to_waypoints

points, cursor = layout_text("你好", TextConfig())
# text_to_waypoints 是 layout_text 的别名
```

## 8. 限制

- 暂不支持标点符号（`!?.,` 等），遇到会抛 `ValueError`；
- 汉字仅限 `hanzi_data.py` 收录的 3755 个 GB2312 一级字；
- 状态文件记录的是**文字排版游标**（基线坐标），不是机器实际位置；
  机器位置由写完后的 `HOME` 回到 `(0, 0, 0)`；
- 大幅修改字号后，旧游标可能不再适用；可用 `--reset` 回到左上角；
- 单线字体的笔画粗细由笔迹决定，与字号无关；
- 逐点 ACK 的协议下，长文本速度受运动时间和 TCP 往返限制；后续可考虑
  PLC 批量路径命令（见 `protocol.md` 第 9 节）。