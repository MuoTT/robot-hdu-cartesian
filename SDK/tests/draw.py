#!/usr/bin/env python3
"""绘图测试脚本：PNG 图片或单线文字 -> 轨迹 -> TCP 下发到 PLC。

用法::

    # PNG -> 轨迹（先离线预览，再连 PLC）
    python tests/draw.py png --input drawing.png --dry-run
    python tests/draw.py png --input drawing.png --dry-run --debug-binary
    python tests/draw.py png --input drawing.png --host 127.0.0.1 --port 2000

    # 手动指定比例与偏移（默认按纸张自动等比居中）
    python tests/draw.py png --input logo.png --scale 0.05 --offset-x 5 --offset-y 5

    # 文字书写（写完自动回零），支持西文/数字/汉字
    python tests/draw.py text "Hello 你好 123" --dry-run
    python tests/draw.py text "Hello 你好" --host 192.168.33.130 --port 2000
    python tests/draw.py text "ABC" --font-size-mm 15 --reset
"""

from __future__ import annotations

import argparse
import os
import sys

# 允许从任意目录直接运行本脚本（把 SDK 根目录加入 sys.path）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from robonix_sdk import (
    PlotConfig,
    RobonixClient,
    build_frame,
    fit_to_paper,
    load_binary,
    to_waypoints,
)
from robonix_sdk import protocol as proto
from robonix_sdk.text import Cursor, TextConfig, TextOverflowError, TextWriter

# 机器行程上限（与 TextConfig.validate 一致）
DEFAULT_PAPER_W = 180.0
DEFAULT_PAPER_H = 150.0


# ---------------------------------------------------------------------------
# 公共参数
# ---------------------------------------------------------------------------
def add_common_args(p: argparse.ArgumentParser, z_down: float) -> None:
    p.add_argument("--host", default="192.168.33.130", help="PLC 地址")
    p.add_argument("--port", type=int, default=2000, help="PLC 端口")
    p.add_argument("--timeout", type=float, default=30.0, help="连接/首点等待超时 (s)")
    p.add_argument("--z-up", type=float, default=-46.0, help="抬笔高度 (mm)，纸面 -50 上方 4 mm")
    p.add_argument("--z-down", type=float, default=z_down, help="落笔高度 (mm)，压在纸上")
    p.add_argument(
        "--vel",
        type=float,
        default=20.0,
        help="运动速度 (mm/s)，通过 SET_PARAM 下发",
    )
    p.add_argument("--acc", type=float, default=300.0, help="加速度 (mm/s²)，SET_PARAM 下发")
    p.add_argument("--dec", type=float, default=300.0, help="减速度 (mm/s²)，SET_PARAM 下发")
    p.add_argument("--jerk", type=float, default=3000.0, help="加加速度，SET_PARAM 下发")
    p.add_argument("--dry-run", action="store_true", help="只生成轨迹，不连接 PLC")
    p.add_argument("--no-home", action="store_true", help="绘制前/结束后都不执行 GroupHome")


# ---------------------------------------------------------------------------
# PNG -> 轨迹
# ---------------------------------------------------------------------------
def make_plot_config(args: argparse.Namespace, img_w: int, img_h: int) -> PlotConfig:
    """按纸张自动适配，或使用显式 --scale / --offset。"""
    if args.scale is not None:
        scale = args.scale
        offset_x = args.offset_x
        offset_y = args.offset_y
        print(
            "手动比例: scale=%.5f mm/px offset=(%.2f, %.2f) mm"
            % (scale, offset_x, offset_y)
        )
    else:
        scale, offset_x, offset_y, draw_w, draw_h = fit_to_paper(
            img_w,
            img_h,
            paper_w_mm=args.paper_width,
            paper_h_mm=args.paper_height,
            margin_mm=args.margin,
            width_mm=args.width_mm,
            height_mm=args.height_mm,
            fill=args.fill,
        )
        print(
            "自动适配: scale=%.5f mm/px offset=(%.2f, %.2f) mm 绘制尺寸=%.1f×%.1f mm"
            % (scale, offset_x, offset_y, draw_w, draw_h)
        )
    return PlotConfig(
        pixel_to_mm=scale,
        offset_x=offset_x,
        offset_y=offset_y,
        z_up=args.z_up,
        z_down=args.z_down,
        vel=args.vel,
        acc=args.acc,
        dec=args.dec,
        jerk=args.jerk,
        flip_x=args.flip_x,
        flip_y=args.flip_y,
        simplify_px=args.simplify_px,
    )


def save_binary_png(binary, path: str) -> None:
    """把 bool 二值图存成黑白 PNG（对应旧版 save_binary）。"""
    from PIL import Image

    Image.fromarray(binary.astype("uint8") * 255, mode="L").save(path)


def print_progress(done: int, total: int, last: tuple[float, float, float]) -> None:
    if done == 1 or done == total or done % 200 == 0:
        print(
            "  进度 %d/%d  最后点 (%.3f, %.3f, %.3f)"
            % (done, total, last[0], last[1], last[2]),
            flush=True,
        )


def check_plot_bounds(points, args: argparse.Namespace) -> bool:
    """打印轨迹实际范围并与纸张比较，返回是否完全在范围内。"""
    if not points:
        return True

    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    xmin, xmax = min(xs), max(xs)
    ymin, ymax = min(ys), max(ys)

    lo_x = args.margin
    hi_x = args.paper_width - args.margin
    lo_y = args.margin
    hi_y = args.paper_height - args.margin

    if lo_x > hi_x or lo_y > hi_y:
        print(
            "[ERROR] 纸张参数非法：margin 超过 paper-width/paper-height",
            file=sys.stderr,
        )
        return False

    print("纸张范围: X[%.1f, %.1f] Y[%.1f, %.1f] mm" % (lo_x, hi_x, lo_y, hi_y))
    print("实际范围: X[%.3f, %.3f] Y[%.3f, %.3f] mm" % (xmin, xmax, ymin, ymax))

    eps = 1e-6
    inside = (
        xmin >= lo_x - eps
        and xmax <= hi_x + eps
        and ymin >= lo_y - eps
        and ymax <= hi_y + eps
    )
    if not inside:
        print(
            "[WARN] 轨迹超出纸张：请调整 --scale / --offset-x / --offset-y，"
            "或设置正确的 --paper-width / --paper-height / --margin",
            file=sys.stderr,
        )
    return inside


def run_png(args: argparse.Namespace) -> int:
    binary = load_binary(args.input, threshold=args.threshold, invert=args.invert)
    h, w = binary.shape
    cfg = make_plot_config(args, w, h)

    if args.debug_binary is not None:
        debug_path = args.debug_binary or (os.path.splitext(args.input)[0] + "_binary.png")
        save_binary_png(binary, debug_path)
        print("二值化图片: %s" % debug_path)

    points = to_waypoints(binary, cfg)

    ink = int(binary.sum())
    down = sum(1 for pt in points if abs(pt[2] - cfg.z_down) < 1e-6)
    up = len(points) - down
    print("图像 %dx%d px  墨迹 %d px  策略=轮廓细描 (marching squares + RDP)" % (w, h, ink))
    print(
        "轨迹点数 %d (落笔 %d / 抬笔 %d)  scale=%.4g mm/px  抽稀=%.3g mm"
        % (len(points), down, up, cfg.pixel_to_mm, cfg.simplify_px * cfg.pixel_to_mm)
    )

    inside = check_plot_bounds(points, args)

    # 打印首帧结构便于核对协议
    if points:
        x0, y0, z0 = points[0]
        frame = build_frame(
            proto.CMD_MOVE_LINEAR,
            0,
            proto.pack_move_linear(x0, y0, z0),
        )
        print("示例 MOVE_LINEAR 帧(%dB): %s" % (len(frame), frame.hex()))

    if args.dry_run:
        print("dry-run 结束（未连接 PLC）")
        return 0

    if not inside:
        print("[ERROR] 轨迹超出纸张，已拒绝下发", file=sys.stderr)
        return 2

    with RobonixClient(args.host, args.port, timeout=args.timeout) as plc:
        print("已连接 %s:%d" % (args.host, args.port))
        plc.set_param(cfg.vel, cfg.acc, cfg.dec, cfg.jerk, cfg.z_up, cfg.z_down)
        if not args.no_home:
            plc.home()          # 绘制前确保轴组已回零/有参考
        plc.draw_waypoints(points, on_progress=print_progress)
        if not args.no_home:
            plc.home()          # 最后回到 Home

    print("完成")
    return 0


# ---------------------------------------------------------------------------
# 文字书写
# ---------------------------------------------------------------------------
def make_text_config(args: argparse.Namespace) -> TextConfig:
    return TextConfig(
        font_size_mm=args.font_size_mm,
        line_spacing=args.line_spacing,
        letter_spacing_mm=args.letter_spacing,
        space_advance_mm=args.space_advance,
        work_width_mm=args.paper_width,
        work_height_mm=args.paper_height,
        margin_mm=args.margin,
        origin_x_mm=args.origin_x,
        reset_y_mm=args.reset_y,
        z_up=args.z_up,
        z_down=args.z_down,
        vel=args.vel,
        acc=args.acc,
        dec=args.dec,
        jerk=args.jerk,
    )


def print_text_plan(text: str, points, cursor_old, cursor_new, cfg: TextConfig) -> None:
    print("文字    : %r" % text)
    print(
        "字号    : %.2f mm, 行高 %.2f mm, 字间距 %.2f mm"
        % (cfg.font_size_mm, cfg.line_height_mm, cfg.letter_spacing_mm)
    )
    print(
        "纸张    : X[0, %.1f] Y[0, %.1f], z_up=%.1f z_down=%.1f"
        % (cfg.work_width_mm, cfg.work_height_mm, cfg.z_up, cfg.z_down)
    )
    print("起点    : (%.3f, %.3f)" % (cursor_old.x_mm, cursor_old.y_mm))

    if points:
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        print(
            "轨迹点  : %d, 边界 X[%.3f, %.3f] Y[%.3f, %.3f]"
            % (len(points), min(xs), max(xs), min(ys), max(ys))
        )
        print("首点    : (%.3f, %.3f, %.3f)" % points[0])
        print("末点    : (%.3f, %.3f, %.3f)" % points[-1])
    else:
        print("轨迹点  : 0（空文本/纯空格）")

    print("新游标  : (%.3f, %.3f)" % (cursor_new.x_mm, cursor_new.y_mm))


def run_text(args: argparse.Namespace) -> int:
    cfg = make_text_config(args)
    writer = TextWriter(cfg, state_path=args.state, reset=args.reset)
    old_cursor = writer.cursor

    try:
        points, new_cursor = writer.plan(args.text)
    except TextOverflowError as e:
        print("[OVERFLOW] %s" % e, file=sys.stderr)
        return 2
    except ValueError as e:
        print("[ERROR] %s" % e, file=sys.stderr)
        return 2

    # 与 TextWriter.write 保存的游标保持一致
    cursor_after = (
        Cursor(cfg.origin_x_mm, new_cursor.y_mm - cfg.line_height_mm)
        if args.new_line
        else new_cursor
    )
    print_text_plan(args.text, points, old_cursor, cursor_after, cfg)

    if args.dry_run:
        print("dry-run 结束（未连接 PLC，未保存游标）")
        return 0

    with RobonixClient(args.host, args.port, timeout=args.timeout) as plc:
        print("已连接 %s:%d" % (args.host, args.port))
        writer.write(
            plc,
            args.text,
            home=not args.no_home,
            next_line=args.new_line,
        )

    print("完成，游标已保存")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="XYZ 绘图测试脚本（PNG / 文字 -> PLC）")
    sub = p.add_subparsers(dest="mode", required=True)

    png = sub.add_parser("png", help="PNG 图片 -> 二值化 -> 轮廓轨迹")
    add_common_args(png, z_down=-50.0)
    png.add_argument("--input", required=True, help="输入 PNG 路径")
    png.add_argument(
        "--scale",
        type=float,
        default=None,
        help="像素->毫米 比例；省略时按纸张自动等比居中（fit_to_paper）",
    )
    png.add_argument(
        "--offset-x", type=float, default=0.0,
        help="图像左上角的世界 X 偏移 (mm)，仅在指定 --scale 时生效",
    )
    png.add_argument(
        "--offset-y", type=float, default=0.0,
        help="图像左上角的世界 Y 偏移 (mm)，仅在指定 --scale 时生效",
    )
    png.add_argument("--threshold", type=float, default=None, help="二值化阈值，默认 Otsu")
    png.add_argument("--invert", action="store_true", help="亮像素视为墨迹")
    png.add_argument("--flip-x", action="store_true")
    png.add_argument("--flip-y", action="store_true")
    png.add_argument(
        "--simplify-px",
        type=float,
        default=1.5,
        help="轮廓 Douglas-Peucker 抽稀容差（像素），0=关闭",
    )
    png.add_argument(
        "--debug-binary",
        nargs="?",
        const="",
        default=None,
        metavar="PATH",
        help="保存二值化图片便于调试；省略 PATH 时输出 <input>_binary.png",
    )
    png.add_argument(
        "--paper-width", "--work-width", dest="paper_width",
        type=float, default=DEFAULT_PAPER_W, help="纸张/行程宽度 (mm)",
    )
    png.add_argument(
        "--paper-height", "--work-height", dest="paper_height",
        type=float, default=DEFAULT_PAPER_H, help="纸张/行程高度 (mm)",
    )
    png.add_argument("--margin", type=float, default=5.0, help="纸张四周留白 (mm)")
    png.add_argument(
        "--width-mm", type=float, default=None,
        help="自动适配时限定绘制宽度 (mm)，默认纸张 80%%",
    )
    png.add_argument(
        "--height-mm", type=float, default=None,
        help="自动适配时限定绘制高度 (mm)，默认纸张 80%%",
    )
    png.add_argument(
        "--fill", type=float, default=0.8,
        help="自动适配时占纸张的比例，默认 0.8",
    )

    txt = sub.add_parser("text", help="单线文字（Hershey 西文 + 汉字笔画中线）")
    add_common_args(txt, z_down=-50.0)
    txt.add_argument("text", help="要写的文字（大小写英文、数字、汉字、空格、换行）")
    txt.add_argument("--font-size-mm", type=float, default=10.0, help="大写字母/汉字墨迹高度 (mm)")
    txt.add_argument("--line-spacing", type=float, default=1.5, help="行高倍数")
    txt.add_argument("--letter-spacing", type=float, default=0.5, help="字间距 (mm)")
    txt.add_argument("--space-advance", type=float, default=None, help="空格宽度 (mm)")
    txt.add_argument(
        "--paper-width", "--work-width", dest="paper_width",
        type=float, default=DEFAULT_PAPER_W,
    )
    txt.add_argument(
        "--paper-height", "--work-height", dest="paper_height",
        type=float, default=DEFAULT_PAPER_H,
    )
    txt.add_argument("--margin", type=float, default=0.0)
    txt.add_argument("--origin-x", type=float, default=5.0, help="每行起笔的绝对 X (mm)")
    txt.add_argument(
        "--reset-y", type=float, default=150.0,
        help="--reset 后文字块顶边的绝对 Y (mm)",
    )
    txt.add_argument("--state", default=None, help="游标状态文件路径")
    txt.add_argument("--reset", action="store_true", help="重置游标到左上角")
    txt.add_argument(
        "--new-line", action="store_true",
        help="写完后游标落到下一行行首（默认停在行尾，下次接着写）",
    )

    args = p.parse_args(argv)
    if args.mode == "png":
        return run_png(args)
    return run_text(args)


if __name__ == "__main__":
    sys.exit(main())