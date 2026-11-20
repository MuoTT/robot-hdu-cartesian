# PLC 侧实现与接线（基于当前 XYZ_Cass 工程）

本文档说明当前 PLC 工程如何接收 SDK 轨迹并驱动三轴机器人。协议细节见
`protocol.md`，SDK 侧用法见 `README.md`。

涉及的 POU（`XYZ_Cass/POUS/Device1/Task0/`）：

| POU | 语言 | 作用 |
|-----|------|------|
| `AxisInit.XML` | FBD | 三轴复位、使能、加组、运动学、轴组使能 |
| `tcp_test.XML` | ST | TCP Server、收包切帧、CRC、命令分发、运动执行、ACK |

---

## 1. 轴与轴组映射

从 `AxisInit` 的 `MC_AddAxisToGroup` 提取：

| 物理轴号 (`Axis`) | 机械轴 | 轴组内序号 (`IdentInGroup`) |
|-------------------|--------|-----------------------------|
| 0 | Z | 2 |
| 1 | Y | 1 |
| 2 | X | 0 |

- SDK 下发的是**轴组世界坐标 (x, y, z)**，对应 `CoordSystem=2`（MCS）。
- 因此 `(x, y, z)` 与 `Target[1]=x, Target[2]=y, Target[3]=z` 直接对应，
  PLC 侧不需要重排。
- 运动学由 `MC_SetKinTransform`（`KinTransform=4`，DenHart 参数
  `dhA/dhD/jdir/jmotor/jshaft/jofs/jlim`）配置，保持 `AxisInit` 原样即可。

---

## 2. 上电/使能时序（AxisInit）

```
1. MC_Reset ×3            → 清除三轴故障
2. MC_Power ×3            → 使能，Mode=8
3. MC_DriverSync          → 驱动器同步
4. MC_AddAxisToGroup ×3   → Z→2、Y→1、X→0 加入轴组 0
5. MC_SetKinTransform     → 载入运动学（KinTransform=4，ExecutionMode=0）
6. MC_GroupEnable         → 轴组使能（AxesGroup=0）
7. tcp_test 状态 10       → SktTCPAccept 监听 2000，等待 SDK 连接
```

`AxisInit` 里没有回零；回零由 `tcp_test` 收到 `HOME(0x03)` 时调用
`MC_GroupHome` 完成。

---

## 3. TCP Server 接线（tcp_test）

功能块实例：

```text
fbAccept : SktTCPAccept;
fbRcv    : SktTCPRcv;
fbSend   : SktTCPSend;
fbClose  : SktTCPClose;
fbStatus : SktGetTCPStatus;
```

关键约定：

- `fbRcv` / `fbSend` / `fbClose` / `fbStatus` **每个扫描周期都调用一次**，
  `Execute` 接状态机维护的请求变量（`RcvReq/SendReq/CloseReq/StatusReq`）；
  这样 FB 内部的 `ExeLast` 每个扫描都能看到完整变化，不会出现“第一次能触发、
  之后卡死”。
- `SktTCPAccept` **不能每扫描调用**（`Execute=FALSE` 分支会关闭监听 socket），
  只在状态 `10`（等待连接）和 `90`（关闭流程）里调用：

```text
fbAccept(Execute := AcceptReq, SrcTcpPort := 2000, TimeOut := 0,
         Done => AcceptDone, Busy =>, Error =>, ErrorID =>,
         Socket0 => Socket0, Socket1 => Socket1);

fbRcv(Execute := RcvReq, Socket0 := Socket0, TimeOut := 10,
      RcvData := ADR(RxBuffer[0]), Size := RxSize, Socket1 := Socket1,
      Done => RcvDone, Busy =>, Error =>, ErrorID =>, RcvSize => RcvSize);

fbSend(Execute := SendReq, Socket0 := Socket0, SendData := ADR(TxBuffer[0]),
       Size := TxSize, Socket1 := Socket1, Done => SendDone, ...);
```

缓冲区：

| 变量 | 大小 | 用途 |
|------|------|------|
| `RxBuffer` | 2048 B | 单次接收缓冲 |
| `RxAccum` | 4096 B | 累积缓冲，处理半包/粘包 |
| `TxBuffer` | 256 B | ACK 组装（4 B，MotionSim 时最多 +200 B） |
| 单帧载荷上限 | 512 B | `LEN > 512` 视为非法，丢 1 字节重同步 |

状态机：

```
0  初始化
10 等待连接（Accept）
20 收数据（Rcv），追加到 RxAccum
30 切帧 / 找 SOF / 校验 LEN 与 CRC，失败丢 1 字节重同步
40 按 CMD 分发并执行
50 组装并发送 ACK
60 收发出错后查询 TCP 连接状态（Status）
90 关闭连接（Close，并以 Accept Execute=FALSE 复位监听）→ 0
99 致命错误 → 90
```

---

## 4. 帧解析

1. 在 `RxAccum` 中找 `SOF = 0xAA 0x55`；没有则只保留末尾可能半个 SOF。
2. 至少 6 B 头后读 `CMD/LEN/SEQ`；`LEN > 512` 判非法，丢 1 B 重找。
3. 整帧长度 `8 + LEN`；不足则等待更多数据（半包）。
4. **CRC16-Modbus**（poly 0x8005，反射，初值 0xFFFF）覆盖
   `CMD..PAYLOAD`（偏移 2 到 `5+LEN`）；与 SDK
   `robonix_sdk.protocol.crc16_modbus` 一致。
5. CRC 错：丢 1 B 重同步，避免把下一个 SOF 一起吞掉。

---

## 5. 命令分发（当前实现）

| CMD | 行为 |
|-----|------|
| `0x00` PING | 直接完成，回 ACK |
| `0x01` MOVE_LINEAR | `LEN >= 24` 时用 `CopyBytes` 把 3×8 B 拷入 `Target[1..3]`（LREAL），`MotionCmd := 1`；否则 `status=2, err=1` |
| `0x03` HOME | `MotionCmd := 2` |
| `0x04` SET_PARAM | `LEN >= 24` 时把 6×4 B 拷入 `Params[1..6]`（REAL），赋给 vel/acc/dec/jerk/z_up/z_down |
| `0x0A` CLOSE | 回 ACK，置 `DisconnectAfterAck`，状态 50 发完后进 90 |
| 其他 | `status=3`（未知命令） |

> SDK 的 `pack_move_linear` 发 3×f64（24 B），`pack_set_param` 发 6×f32
> （24 B），与 PLC 的 `LREAL` / `REAL` 布局一致。

---

## 6. 运动执行

```text
MC_MoveLinearAbsolute1(
    AxesGroup := 0,
    Execute := MoveExec,              // MotionCmd = 1 时触发
    Position := ADR(Target[1]),       // ARRAY[1..3] OF LREAL
    Velocity := vel,                  // REAL，默认 15
    Acceleration := acc,              // REAL，默认 300
    Deceleration := dec,              // REAL，默认 300
    Jerk := jerk,                     // REAL，默认 3000
    CoordSystem := 2,                 // MCS
    BufferMode := 1,
    TransitionMode := 0,
    TransitionParameter := 0.0);
```

```text
MC_GroupHome1(
    AxesGroup := 0,
    Execute := HomeExec,              // MotionCmd = 2 时触发
    Position := ADR(Home[1]),         // ARRAY[1..3] OF LREAL := [0,0,0]
    CoordSystem := 2,
    BufferMode := 0);
```

- 联调模式：`MotionSim = TRUE` 时只做 TCP 联调，运动 FB 不触发、命令直接 ACK；
  正式绘图必须 `MotionSim = FALSE`。
- 运动完成判定：先看到 `Active/Busy`（`MotionSeenBusy := TRUE`），再看到
  `Done` 才回 ACK；`Error` 则回 `status=2 + ErrorID`。
- `MOVE_LINEAR` / `HOME` 都是同步语义：PLC 在运动结束后才回 ACK。
- 同一时刻只允许一条运动命令在途，`BufferMode` 实际不生效；连续插补需要
  新增批量路径命令。

---

## 7. 变量与默认值

| 变量 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `vel` | REAL | 15 | 直线运动速度，可由 `SET_PARAM` 修改 |
| `acc` / `dec` | REAL | 300 | 加减速 |
| `jerk` | REAL | 3000 | 加加速度 |
| `z_up` / `z_down` | REAL | 0 / -25 | 当前未被 SDK 使用（z 已编进坐标） |
| `Home` | `ARRAY[1..3] OF LREAL` | `[0,0,0]` | 回零目标 |
| `Target` | `ARRAY[1..3] OF LREAL` | — | 绝对目标位置 |
| `RxSize` | UINT | 2048 | `fbRcv` 单次读取上限 |

---

## 8. 安全与排障

- 首次联调先用 `MotionSim = TRUE` 验证帧格式与 ACK，再切真实运动。
- 先低速（`vel = 5~10`）空跑，确认 X/Y/Z 方向、原点和行程（X 180、Y 150）。
- 客户端退出必须收到 `CMD_CLOSE(0x0A)` 的 ACK；直接断 socket 会让 PLC 卡在
  `20 <-> 60` 死循环。恢复方法：重启 PLC，或把 CASS 里的 `Tcp.TCP_State` 置 0。
- ACK 超时先检查：PLC 是否在运行 `tcp_test`、`Task0` 周期是否过大、
  是否卡在旧连接（可先重连）。