# Robonix XYZ 绘图 —— 通信协议说明

本文档定义 PC 端 SDK（Python，下称 **Client**）与 PLC（MatrixBox MX4001，
下称 **Server**）之间的 TCP 二进制通信协议。PLC 侧实现在
`XYZ_Cass/POUS/Device1/Task0/tcp_test.XML`（功能块 `SktTCPAccept` /
`SktTCPRcv` / `SktTCPSend`）。

当前 PLC 地址：`192.168.1.135`，端口 `2000`。

---

## 1. 传输层

| 项 | 约定 |
|----|------|
| 协议 | TCP |
| 角色 | **PLC = Server**（`SktTCPAccept` 监听），**SDK = Client** 主动连接 |
| 默认端口 | **2000** |
| 字节序 | **小端**（little-endian，与 Intel 平台 PLC 一致） |
| 浮点 | 位置/坐标（`MOVE_LINEAR` 的 x/y/z）为 IEEE 754 **float64**，对应 PLC `LREAL`；运动参数（`SET_PARAM` 的 6 个参数）为 **float32**，对应 PLC `REAL` |

推荐 PLC 侧 `SktTCPRcv.TimeOut` 为 `10`（10 ms），`SktTCPAccept.TimeOut` 为 `0`
（无限等待连接）。单帧载荷上限 **512 B**，整帧上限 **520 B**。

---

## 2. 帧格式

```
偏移        长度      字段
0           2        SOF  帧头 = 0xAA 0x55
2           1        CMD  命令码 (U8)
3           2        LEN  载荷长度 (U16, LE)
5           1        SEQ  序号 (U8, 0..255，应答原样带回)
6           LEN      PAYLOAD
6+LEN       2        CRC  CRC16-Modbus (U16, LE)
```

- 帧总长 = `6 + LEN + 2` 字节。
- **CRC16-Modbus**（多项式 0x8005，反射，初值 0xFFFF），
  计算范围为 `CMD(1) + LEN(2) + SEQ(1) + PAYLOAD(LEN)`，即偏移 2 到 `6+LEN-1`。
  SOF 与 CRC 自身不参与校验。
- 命令方向：**C→S** 表示 SDK→PLC，**S→C** 表示 PLC→SDK。

---

## 3. 命令表与实现状态

状态以当前 `tcp_test.XML` 为准。SDK 只暴露已实现的命令。

| CMD  | 名称          | 方向 | 载荷 | 状态 |
|------|---------------|------|------|------|
| 0x00 | PING          | C→S  | 空 | 已实现，回 ACK |
| 0x01 | MOVE_LINEAR   | C→S  | `x, y, z = 3×f64`（**24 B**） | 已实现；MCS 绝对直线插补，速度由 `SET_PARAM` 的 `vel` 决定 |
| 0x03 | HOME          | C→S  | 空 | 已实现（`MC_GroupHome`） |
| 0x04 | SET_PARAM     | C→S  | `vel, acc, dec, jerk, z_up, z_down = 6×f32`（**24 B**） | 已实现；设置全局运动参数 |
| 0x0A | CLOSE         | C→S  | 空 | 已实现，ACK 后关闭连接 |
| 0x80 | ACK           | S→C  | `status U8 + err U16 + busy U8`（4 B） | 所有命令的应答，SEQ 回带 |

---

## 4. ACK 应答

每个 C→S 命令 PLC 处理完成后回一帧 `ACK`，`SEQ` 与请求一致，用于逐帧握手。

```
ACK 载荷 (4B):
  status  U8   0=OK, 1=忙, 2=错误, 3=未知命令
  err     U16  错误码（status=2 时有效，对应 PLC 的 ErrorID）
  busy    U8   0=空闲, 1=轴组忙
```

> **MotionSim 调试模式**：ACK 载荷 = 4 B `status/err/busy` + 原始请求帧
> （最多 200 B，含 SOF/CMD/LEN/SEQ/PAYLOAD/CRC）。客户端只读前 4 B，
> 不影响握手；抓包时可以看到 PLC 实际收到的完整输入帧。

---

## 5. 交互模型

### 5.1 逐帧同步握手

SDK 每发一条命令，等待 PLC 返回对应 `SEQ` 的 ACK 后再发下一条。
`MOVE_LINEAR` 和 `HOME` 的 ACK 在运动 `Done`（或 `Error`）之后才发出，
因此这些命令是**同步语义**，不是“收到即返回”。

`RobonixClient.draw_waypoints` 按行程估算每点超时：

```
speed = 5.0                              # 保守估计 mm/s（PLC 默认 vel=15）
timeout = min(300.0, max(self.timeout, 2.0 + distance / speed))
首点固定 30 s（等轴组启动/首段运动）
```

行程长或 PLC 速度设置得很慢时，应增大构造函数的 `timeout`（作为每点超时下限）。

### 5.2 关闭会话

退出 `with RobonixClient(...)` 时会先发 `CMD_CLOSE(0x0A)` 并等待 ACK（3 s 兜底），
再关闭 socket。**不能直接断开 socket**：PLC 的 `SktTCPRcv` 会持续报错，但
`SktGetTCPStatus` 仍认为连接有效，状态机卡在 `20 <-> 60` 死循环，之后不再
`Accept` 新连接。遇到这种情况需重启 PLC，或把 CASS 里的 `Tcp.TCP_State` 置 0。

### 5.3 运动与笔高度

`MOVE_LINEAR` 是 **MCS 绝对坐标**直线插补，需保证轴组已回零/有有效参考位置。
抬笔与落笔都是同一条命令，区别只在 `z`：

- `z = z_down = -50`：落笔（绘制段）；
- `z = z_up = -46`：抬笔（空行程，离纸 4 mm）。

速度由 `SET_PARAM` 的 `vel` 决定：PLC 的 `MC_MoveLinearAbsolute.Velocity`
接的是 PLC 变量 `vel`（REAL，默认 15）。未收到 `SET_PARAM` 时使用该默认值。

---

## 6. 坐标与单位

| 项 | 约定 |
|----|------|
| 单位 | 毫米（mm） |
| 纸面原点 | 纸张左下角 `(0, 0)` |
| 方向 | `+X` 向右，`+Y` 向上 |
| 机器零点 | 纸面左下角上方 50 mm，即 `(0, 0, 0)` |
| 纸面 | `z = -50` |
| 落笔 / 抬笔 | `z_down = -50` / `z_up = -46` |
| 行程上限 | `X 180 mm`、`Y 150 mm` |

`z_up/z_down` 由 SDK 直接编入坐标，PLC 无需区分提笔/落笔。

---

## 7. 载荷编解码示例

### MOVE_LINEAR（24 B 载荷，整帧 32 B）

```python
x, y, z = 10.0, 20.0, -46.0
payload = struct.pack('<3d', x, y, z)   # robonix_sdk.protocol.pack_move_linear
```

PLC 写入 `Target[1..3]`：

```text
Target[1] = x（LREAL）
Target[2] = y
Target[3] = z
```

### SET_PARAM（24 B 载荷）

```python
vel, acc, dec, jerk, z_up, z_down = 20.0, 300.0, 300.0, 3000.0, -46.0, -50.0
payload = struct.pack('<6f', vel, acc, dec, jerk, z_up, z_down)
# robonix_sdk.protocol.pack_set_param(vel, acc, dec, jerk, z_up, z_down)
```

SDK 与 PLC 均按 `6×f32`（`REAL`）解析，长度 24 B。

### 完整帧示例（PING，SEQ=1）

```
CMD=0x00 LEN=0x0000 SEQ=0x01
CRC 对 [00 00 00 01] 计算，得 0xC0C1
帧 = AA 55 00 00 00 01 C0 C1
```

（实际字节以 `robonix_sdk.protocol.build_frame` 输出为准。）

---

## 8. PLC 侧实现要点

`tcp_test.XML` 的状态机：

```
0 初始化 → 10 等待连接 → 20 收数据 → 40 解析/执行 → 50 发送 ACK
         → 20 / 30（粘包）→ 60 查询连接状态 → 20 / 90 关闭 → 0
```

1. `SktTCPAccept` 只在状态 10 / 90 调用（`Execute=FALSE` 会关闭监听 socket），
   `SrcTcpPort=2000`、`TimeOut=0`。
2. `SktTCPRcv`（`fbRcv`）每扫描调用，`TimeOut=10`，收到数据追加到 `RxAccum`
   （4096 B，接收缓冲 2048 B）。
3. 切帧：找 `SOF=0xAA55`，校验 `LEN <= 512`，按 `6+LEN+2` 取整帧，
   用同一套 **CRC16-Modbus** 校验；失败时丢弃 1 字节重新同步。
4. 分发：
   - `0x01`：`PayloadLen >= 24` 时用 `CopyBytes` 把 3×8 B 拷入
     `Target[1..3]`（`ARRAY[1..3] OF LREAL`），拉高 `MC_MoveLinearAbsolute.Execute`；
   - `0x03`：`MC_GroupHome`，`Position = ADR(Home[1])`；
   - `0x04`：6×4 B → `Params[1..6]`（REAL）；
   - 其他：`status=3`（未知命令）。
5. 运动 FB：

```text
MC_MoveLinearAbsolute(
    AxesGroup := 0, Execute := MoveExec,
    Position := ADR(Target[1]), Velocity := vel,
    Acceleration := acc, Deceleration := dec, Jerk := jerk,
    CoordSystem := 2, BufferMode := 1, TransitionMode := 0);
```

6. `Done` / `Error` 后组装 4 B ACK（MotionSim 模式附带原始帧），
   `SktTCPSend` 发出；`0x0A` 在 ACK 发送完成后进入状态 90 关闭连接。

---

## 9. 已知限制

- **无批量路径命令**：逐点同步握手，同一时刻只有一条运动指令在途，
  PLC 的 `BufferMode` 实际不生效，连续插补需要新增 `MOVE_PATH` 类命令。
- **SET_PARAM 是全局参数**：一次设置会影响后续所有 `MOVE_LINEAR`，
  直到再次设置或 PLC 重启。
- **逐点 ACK 的吞吐**：长轨迹受运动时间和 TCP 往返限制，点数多时建议先抽稀。