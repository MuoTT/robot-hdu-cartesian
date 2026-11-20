"""RobonixClient —— 与 PLC 的 TCP 会话层（Robonix primitive 也复用它）。

语义要点（换协议/换厂家时最容易踩的几条）：

1. 角色：PLC 是 Server（SktTCPAccept 监听 2000），本类是 Client；
2. 逐帧同步握手：发一帧 -> 等同一 SEQ 的 ACK -> 才发下一帧。
   PLC 的 ACK 是在**运动 Done 之后**才回的，所以 draw_waypoints 的
   每点超时按「距离 / 保守速度」估算，而不是固定值；
3. 关闭：退出 with 时先发 CMD_CLOSE(0x0A) 再断 socket。
   直接断 socket 会让 PLC 卡在 20 <-> 60 死循环，此后必须重启 PLC
   或把 Tcp.TCP_State 置 0；
4. 运动参数：速度/加减速通过 SET_PARAM(0x04, 6×f32) 设置，本类不自动下发；
   调用方在开始运动前调用 set_param 即可。未下发时 PLC 使用自身常量；
5. 逐点在途：同一时刻只有一条运动指令，PLC 侧 BufferMode 因此不生效；
   要做连续插补需要批量命令（MOVE_PATH），见 docs/protocol.md。
"""

from __future__ import annotations

import math
import socket
import sys
import time
from collections.abc import Callable, Iterable

from . import protocol as proto

DEFAULT_PORT = 2000

# 逐点 ACK 超时估算用的保守速度 (mm/s)。PLC 默认 vel=15，
# 这里取更慢的值，宁可多等也不误判超时。
_TIMEOUT_SPEED_MM_S = 5.0


class ProtocolError(Exception):
    """协议层错误（非法帧 / CRC 失败 / 应答错误）。"""


class RobonixClient:
    def __init__(self, host: str, port: int = DEFAULT_PORT, timeout: float = 5.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._seq = 0
        self._buf = b""

    # ------------------------------------------------------------------
    # 连接管理
    # ------------------------------------------------------------------
    def connect(self) -> None:
        sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        sock.settimeout(self.timeout)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._sock = sock
        self._buf = b""

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def __enter__(self) -> "RobonixClient":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        # 优雅关闭：先发 CMD_CLOSE，让 PLC 走 50 -> 90 -> 0 -> 10 的正常流程。
        # 直接关 socket 的话，PLC 的 SktTCPRcv 会一直报错，但 SktGetTCPStatus
        # 仍认为连接有效，于是卡在 20 <-> 60 死循环，永远不再 Accept 新连接
        # （表象：客户端"已连接"但再也收不到任何 ACK，必须重新下装 PLC）。
        try:
            self.close_session()
        except Exception as e:  # noqa: BLE001 - 关闭阶段任何异常都不应抛出
            print(
                "[WARN] 会话未正常关闭（%s: %s），已直接断开；"
                "若之后连不上 PLC，请在 CASS 里把 Tcp.TCP_State 置 0"
                % (type(e).__name__, e),
                file=sys.stderr,
            )
        finally:
            self.close()

    @property
    def connected(self) -> bool:
        return self._sock is not None

    # ------------------------------------------------------------------
    # 底层收发
    # ------------------------------------------------------------------
    def _set_timeout(self, seconds: float) -> None:
        """临时改 socket 接收超时（长行程运动需要更久）。"""
        if self._sock is not None:
            self._sock.settimeout(max(0.5, float(seconds)))

    def _next_seq(self) -> int:
        seq = self._seq
        self._seq = (self._seq + 1) & 0xFF
        return seq

    def _send(self, cmd: int, payload: bytes = b"") -> int:
        if self._sock is None:
            raise RuntimeError("未连接，请先调用 connect()")
        seq = self._next_seq()
        frame = proto.build_frame(cmd, seq, payload)
        self._sock.sendall(frame)
        return seq

    def _recv_frame(self) -> proto.Frame:
        if self._sock is None:
            raise RuntimeError("未连接")
        while True:
            frame, consumed = proto.parse_frame(self._buf)
            if frame is not None:
                self._buf = self._buf[consumed:]
                return frame
            try:
                chunk = self._sock.recv(4096)
            except socket.timeout:
                raise TimeoutError(
                    "等待 PLC 应答超时（当前 socket 超时 %.1fs）。"
                    "MOVE_LINEAR 是同步语义，PLC 要等这段运动走完才回 ACK；"
                    "行程长或速度慢时请调大 timeout。" % (self._sock.gettimeout() or 0)
                )
            if not chunk:
                raise ConnectionError("连接被对端关闭")
            self._buf += chunk

    def _request(self, cmd: int, payload: bytes = b"") -> proto.Frame:
        """发送一帧并等待对应 seq 的 ACK，返回 ACK 帧。"""
        seq = self._send(cmd, payload)
        while True:
            frame = self._recv_frame()
            if frame.cmd != proto.CMD_ACK:
                continue
            if frame.seq != seq:
                continue
            return frame

    def _expect_ack_ok(self, cmd: int, payload: bytes = b"", what: str = "命令") -> tuple[int, int, int]:
        ack = self._request(cmd, payload)
        status, err, busy = proto.unpack_ack(ack.payload)
        if status != proto.STATUS_OK:
            raise ProtocolError(
                "%s失败: status=%d err=0x%04X busy=%d" % (what, status, err, busy)
            )
        return status, err, busy

    # ------------------------------------------------------------------
    # 高层命令
    # ------------------------------------------------------------------
    def ping(self) -> bool:
        try:
            self._expect_ack_ok(proto.CMD_PING, what="PING")
            return True
        except (ProtocolError, TimeoutError, ConnectionError):
            return False

    def move_linear(self, x: float, y: float, z: float) -> None:
        """世界坐标直线插补。速度由之前的 SET_PARAM 决定。"""
        self._expect_ack_ok(
            proto.CMD_MOVE_LINEAR,
            proto.pack_move_linear(x, y, z),
            what="MOVE_LINEAR",
        )

    def set_param(
        self, vel: float, acc: float, dec: float, jerk: float, z_up: float, z_down: float
    ) -> None:
        """CMD_SET_PARAM(0x04)：vel, acc, dec, jerk, z_up, z_down（6×f32）。

        参数在 PLC 侧全局生效，直到下一次 SET_PARAM 或 PLC 重启。
        z_up/z_down 对当前 PLC 的运动没有影响（z 已编进每个坐标）。
        """
        self._expect_ack_ok(
            proto.CMD_SET_PARAM,
            proto.pack_set_param(vel, acc, dec, jerk, z_up, z_down),
            what="SET_PARAM",
        )

    def home(self) -> None:
        self._expect_ack_ok(proto.CMD_HOME, what="HOME")

    def close_session(self) -> None:
        """请求 PLC 关闭当前会话（CMD_CLOSE 0x0A），确认后断开本地连接。

        PLC 收到 0x0A 会回 ACK，然后走 50 -> 90 -> 0 -> 10 的正常关闭流程，
        重新进入 Accept。这一步不能省：如果客户端直接关 socket，PLC 的
        SktTCPRcv 会持续报错，而 SktGetTCPStatus 仍判定连接有效，
        状态机就会在 20 <-> 60 之间死循环，再也不 Accept 新连接。

        等待 ACK 用 3 秒兜底：PLC 忙或已卡住时也不至于卡死退出流程。
        """
        if self._sock is None:
            return
        old_timeout = self.timeout
        self._set_timeout(3.0)
        try:
            self._expect_ack_ok(proto.CMD_CLOSE, what="CLOSE")
        except TimeoutError:
            raise TimeoutError(
                "PLC 未在 3 秒内确认 CLOSE（可能卡在运动中或已失联）"
            ) from None
        finally:
            self._set_timeout(old_timeout)
            self.close()

    # ------------------------------------------------------------------
    # 绘图
    # ------------------------------------------------------------------
    def draw_waypoints(
        self,
        points: Iterable[tuple[float, float, float]],
        on_progress: Callable[[int, int, tuple[float, float, float]], None] | None = None,
    ) -> None:
        """逐点下发 MOVE_LINEAR（逐点握手）。

        参数
        ----
        points:
            ``(x, y, z)`` 序列，z 值已编码笔状态。
        on_progress:
            回调 ``(done, total, last_point)``，可打印进度。

        每点超时按「距离 / 保守速度」估算；PLC 未提供速度回读，
        这里固定按 5 mm/s 估算（实际默认 vel=15），并把构造函数的
        ``timeout`` 作为下限。
        """
        pts = list(points)
        total = len(pts)
        prev: tuple[float, float, float] | None = None
        for i, (x, y, z) in enumerate(pts):
            # MOVE_LINEAR 是同步语义：PLC 等这段运动真正走完才回 ACK。
            # 所以超时按「本段距离 / 速度」估算，而不是用固定的 5 秒。
            if prev is None:
                self._set_timeout(max(self.timeout, 30.0))
            else:
                dist = math.dist(prev, (x, y, z))
                self._set_timeout(
                    min(300.0, max(self.timeout, 2.0 + dist / _TIMEOUT_SPEED_MM_S))
                )
            self.move_linear(x, y, z)
            prev = (x, y, z)
            if on_progress is not None:
                on_progress(i + 1, total, (x, y, z))
