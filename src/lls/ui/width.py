"""字符在终端里占几列。

终端用「列」而不是字符数计算位置：宽字符（CJK、全角标点）占两列，组合符
占零列。排版、光标定位、截断都必须按这个宽度算，否则一行会比实际窄或宽，
后续的行列对应关系随之整体错位。

模糊宽度（East Asian Ambiguous）是唯一没有统一答案的部分：Unicode 把决定权
留给终端，同一段文本在不同终端上宽度不同。:func:`conservative_char_width`
按两列算，对覆盖层这类**按行号绝对定位**的排版是安全的选择。
"""

from __future__ import annotations

import unicodedata
from typing import Callable

__all__ = ["char_width", "conservative_char_width", "text_width"]

# 码点区间上界 → 显示宽度。二分不必：表很短，且绝大多数查询落在前几项。
_WIDTHS = (
    (126, 1), (159, 0), (687, 1), (710, 0), (711, 1),
    (727, 0), (733, 1), (879, 0), (1154, 1), (1161, 0),
    (4347, 1), (4447, 2), (7467, 1), (7521, 0), (8369, 1),
    (8426, 0), (9000, 1), (9002, 2), (11021, 1), (12350, 2),
    (12351, 1), (12438, 2), (12442, 0), (19893, 2), (19967, 1),
    (55203, 2), (63743, 1), (64106, 2), (65039, 1), (65059, 0),
    (65131, 2), (65279, 1), (65376, 2), (65500, 1), (65510, 2),
    (120831, 1), (262141, 2), (1114109, 1),
)

Measure = Callable[[str], int]


def char_width(char: str) -> int:
    """字符占几列。"""
    code = ord(char)
    if code == 0x0E or code == 0x0F:
        return 0
    for limit, width in _WIDTHS:
        if code <= limit:
            return width
    return 1


def conservative_char_width(char: str) -> int:
    """模糊宽度按两列算的 :func:`char_width`。"""
    if unicodedata.east_asian_width(char) == "A":
        return 2
    return char_width(char)


def text_width(text: str, measure: Measure = char_width) -> int:
    """字符串的显示宽度。"""
    return sum(measure(char) for char in text)
