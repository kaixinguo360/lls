"""工具参数的类型收敛。

模型交来的是 JSON，什么都可能：``"10"`` 而不是 ``10``、``NaN``、缺字段。
凡是「按整数/浮点解释，不行就用默认值」的地方都走这里，免得每个工具各写
一份 try/except，也免得某处漏掉 ``NaN`` 这种能一路钻进循环的怪物。
"""

from __future__ import annotations

import math
from typing import Any

__all__ = ["as_float", "as_int"]


def as_int(value: Any, default: int) -> int:
    """解释成整数；不行（缺字段、字符串、空值）就用 ``default``。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def as_float(value: Any, default: float) -> float:
    """解释成浮点；不行、或不是有限数（``NaN`` / ``inf``）就用 ``default``。

    非有限数必须挡住：``deadline = now + NaN`` 会让所有比较都假，等循环
    于是永远等下去。
    """
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(parsed):
        return default
    return parsed
