"""字符在终端里占几列。

终端按列排版：宽字符（CJK、全角标点）占两列，组合符、变体选择符、ZWJ 占零
列。宽度来自本模块自己的区间表 ``_WIDTHS``：十来行，一眼看得全，也不随某
个包的版本悄悄换答案。表只按码点切，兜不住的另按 Unicode 类别补规则：组合符
与格式符（含 VS16、ZWJ）零列，emoji 肤色修饰零列，控制符零列但仍独立成簇，
带 VS16 的簇按两列 —— 那是 emoji 呈现形式，终端画两列。

排版的单位是**簇**（grapheme cluster），不是字符：基字符连同其后的零宽修饰
（组合符、VS16、ZWJ 后面的那个字符、成对的区域指示符）算一个整体。逐字符相
加会把「窄字符 + VS16」算成一列（终端画的是两列）、把家庭 emoji 算成六列，
截断时还可能把修饰符和它的基字符切成两半 —— 终端于是画出半个字符，行列全乱。

宽度只有一份：pyte 的屏幕模型也读这张表（:mod:`lls.term.pyte_adapter` 把
:func:`conservative_char_width` 注入 ``pyte.screens.wcwidth``）。排版与模型
给出同一个答案，覆盖层折出的行数才和模型里的一样 —— 这是按行绝对定位的前提。

模糊宽度（East Asian Ambiguous）是唯一没有统一答案的部分：Unicode 把决定权
留给终端，同一段文本在不同终端上宽度不同。:func:`conservative_char_width`
按两列算，对覆盖层这类**按行号绝对定位**的排版是安全的选择。
"""

from __future__ import annotations

import re
import unicodedata
from typing import Callable, Iterator

__all__ = [
    "char_width",
    "cluster_end",
    "clusters",
    "conservative_char_width",
    "strip_sgr",
    "text_width",
]

# 覆盖层里允许出现 SGR（加粗、颜色）序列：它们不占列，量宽度时必须先去掉。
_SGR = re.compile("\x1b\\[[0-9;]*m")

Measure = Callable[[str], int]

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

_ZWJ = "\u200d"
_RI_FIRST = "\U0001f1e6"
_RI_LAST = "\U0001f1ff"
_VS16 = "\ufe0f"
_MODIFIER_FIRST = "\U0001f3fb"
_MODIFIER_LAST = "\U0001f3ff"

# 零列的类别：组合符、包围符、格式符（含 VS16、ZWJ）、控制符。
_ZERO_CATEGORIES = ("Mn", "Me", "Cf", "Cc")


def _table_width(char: str) -> int:
    """一个码点在区间表里的宽度。"""
    code = ord(char)
    for limit, width in _WIDTHS:
        if code <= limit:
            return width
    return 1


def _is_zero(char: str) -> bool:
    """这个字符自己占零列（与它是否并进上一簇无关，见 :func:`_is_control`）。"""
    if _MODIFIER_FIRST <= char <= _MODIFIER_LAST:
        return True
    return unicodedata.category(char) in _ZERO_CATEGORIES


def char_width(cluster: str) -> int:
    """一个字符（或一整个簇）占几列。

    簇宽取第一个真正占列的字符：ZWJ 序列与基字符加修饰都以前者为准。带 VS16
    的簇按两列 —— 那是 emoji 呈现形式，终端画两列，表里只有一列。
    """
    width = 0
    for char in cluster:
        if _is_zero(char):
            continue
        if not width:
            width = _table_width(char)
    if _VS16 in cluster:
        width = max(width, 2)
    return width


def conservative_char_width(cluster: str) -> int:
    """模糊宽度按两列算的 :func:`char_width`。

    只看真正占列的字符：组合符、ZWJ 的宽度由基字符决定，不该再被放大。
    """
    width = char_width(cluster)
    if not width:
        return 0
    for char in cluster:
        if char_width(char) and unicodedata.east_asian_width(char) == "A":
            return max(width, 2)
    return width


def _is_regional(char: str) -> bool:
    return _RI_FIRST <= char <= _RI_LAST


def _is_control(char: str) -> bool:
    """控制字符（ESC、BEL 之类）：它们零列，但不是修饰符。"""
    return unicodedata.category(char) == "Cc"


def cluster_end(text: str, index: int) -> int:
    """``text[index:]`` 里第一个簇在哪里结束（不含）。"""
    end = index + 1
    regional = 1 if _is_regional(text[index]) else 0
    while end < len(text):
        char = text[end]
        if text[end - 1] == _ZWJ:
            joins = True
        elif _is_regional(char):
            # 区域指示符两个一对：第三个得另起一个簇。
            joins = regional % 2 == 1
        else:
            joins = not _is_control(char) and _is_zero(char)
        if not joins:
            break
        if _is_regional(char):
            regional += 1
        end += 1
    return end


def clusters(text: str) -> Iterator[str]:
    """把文本切成簇。SGR 序列不在这里处理（见 :func:`strip_sgr`）。"""
    index = 0
    while index < len(text):
        end = cluster_end(text, index)
        yield text[index:end]
        index = end


def strip_sgr(text: str) -> str:
    """去掉 SGR（颜色、加粗）转义序列，只留可见字符。"""
    return _SGR.sub("", text)


def text_width(text: str, measure: Measure = char_width) -> int:
    """字符串的显示宽度（SGR 序列按零列算）。"""
    return sum(measure(cluster) for cluster in clusters(strip_sgr(text)))
