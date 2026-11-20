"""XYZ 绘图机器人 SDK（上层库）。

这套库只做两件事，且两层互不依赖：

    1. 几何层（不碰网络）：image / trajectory / text
       - image      : PNG -> Otsu 二值化
       - trajectory : 二值图 -> 轮廓轨迹点（毫米）
       - text       : 文字 -> 单线轨迹点（Hershey 西文 + 汉字笔画中线）

    2. 会话层（只碰硬件）：protocol / client
       - protocol   : 二进制帧 + CRC16-Modbus
       - client     : TCP 客户端，逐帧同步握手（发一条、等对应 SEQ 的 ACK）

坐标与单位（全库统一）：
    - 毫米，原点在纸面左下角，+X 向右、+Y 向上；
    - 机器零点 (0,0,0) = 纸面左下角上方 50 mm；
    - 纸面 z = -50，落笔 z_down = -50，抬笔 z_up = -46。

与 Robonix 的关系：
    接入 Robonix 时，几何层进 skill 包（只通过标准能力控制硬件），
    会话层进 primitive 包（把 TCP 帧翻译成 robonix/primitive/arm/* 契约）；
    标准契约用米，换算 (mm / 1000.0) 放在 primitive 边界做，这里保持毫米。

用法示例::

    from robonix_sdk import RobonixClient, PlotConfig, load_binary, to_waypoints, fit_to_paper

    binary = load_binary("drawing.png")
    scale, ox, oy, w, h = fit_to_paper(binary.shape[1], binary.shape[0])
    points = to_waypoints(binary, PlotConfig(pixel_to_mm=scale, offset_x=ox, offset_y=oy))

    with RobonixClient("192.168.33.130") as plc:
        # 速度/加减速由 SET_PARAM(6×f32) 下发；z_up/z_down 已编进坐标
        plc.set_param(15.0, 300.0, 300.0, 3000.0, -46.0, -50.0)
        plc.home()
        plc.draw_waypoints(points)
"""

from .protocol import (
    CMD_ACK,
    CMD_CLOSE,
    CMD_HOME,
    CMD_MOVE_LINEAR,
    CMD_PING,
    CMD_SET_PARAM,
    STATUS_BUSY,
    STATUS_ERROR,
    STATUS_OK,
    STATUS_UNKNOWN_CMD,
    Frame,
    build_frame,
    cmd_name,
    crc16_modbus,
    parse_frame,
)
from .client import DEFAULT_PORT, ProtocolError, RobonixClient
from .image import binarize, load_binary, load_gray, otsu_threshold
from .trajectory import PlotConfig, fit_to_paper, outline_waypoints, to_waypoints

__all__ = [
    "RobonixClient",
    "ProtocolError",
    "DEFAULT_PORT",
    "PlotConfig",
    "to_waypoints",
    "outline_waypoints",
    "fit_to_paper",
    "load_gray",
    "load_binary",
    "binarize",
    "otsu_threshold",
    "Frame",
    "build_frame",
    "parse_frame",
    "crc16_modbus",
    "cmd_name",
    "CMD_PING",
    "CMD_MOVE_LINEAR",
    "CMD_HOME",
    "CMD_SET_PARAM",
    "CMD_CLOSE",
    "CMD_ACK",
    "STATUS_OK",
    "STATUS_BUSY",
    "STATUS_ERROR",
    "STATUS_UNKNOWN_CMD",
]

__version__ = "0.1.0"
