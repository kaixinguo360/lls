"""录制容器格式：版本、通道、分段名与 resize 载荷。

一个录制目录里，``meta.json`` 描述整体，``segment-000001.cast`` 是 JSONL：
第一行是段头对象，之后每行是 ``[段内秒数, 通道, base64(原始字节)]``。
压缩开启时已关闭的分段名为 ``segment-000001.cast.gz``。
"""

from __future__ import annotations

import re

__all__ = [
    "CHANNEL_INPUT",
    "CHANNEL_OUTPUT",
    "CHANNEL_RESIZE",
    "CHANNELS",
    "FORMAT_VERSION",
    "SEGMENT_RE",
    "TIME_EPSILON",
    "absolute_time",
    "parse_resize",
]

FORMAT_VERSION = 2

#: 索引里的 at 与帧时间各自四舍五入到微秒，seek 时留一点余量。
TIME_EPSILON = 1e-6

CHANNEL_INPUT = "i"
CHANNEL_OUTPUT = "o"
CHANNEL_RESIZE = "r"
CHANNELS = (CHANNEL_INPUT, CHANNEL_OUTPUT, CHANNEL_RESIZE)

#: ``segment-000001.cast`` / ``segment-000001.cast.gz``。
SEGMENT_RE = re.compile(r"^segment-(\d+)\.cast(\.gz)?$")


def absolute_time(time_base: float, relative: float) -> float:
    """段内时间 → 录制起点起的绝对秒数。"""
    return time_base + relative


def parse_resize(data: bytes) -> tuple[int, int] | None:
    """解析 ``"{COLS}x{ROWS}"``，返回 ``(rows, columns)``；坏载荷返回 ``None``。"""
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError:
        return None
    columns, sep, rows = text.partition("x")
    if not sep:
        return None
    try:
        width = int(columns)
        height = int(rows)
    except ValueError:
        return None
    if width <= 0 or height <= 0:
        return None
    return height, width
