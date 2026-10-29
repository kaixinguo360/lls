"""字符在终端里占几列。

终端按列排版：宽字符（CJK、全角标点）占两列，组合符、变体选择符、ZWJ 占零
列。宽度数据交给 wcwidth —— 它按 Unicode 的 East Asian Width 与 Zero Width
表给出答案，pyte 算屏幕模型用的也是它。自己抄一份表只会多一套会过期的答案。

排版的单位是**簇**（grapheme cluster），不是字符：基字符连同其后的零宽修饰
（组合符、VS16、ZWJ 后面的那个字符、成对的区域指示符）算一个整体。逐字符相
加会把「窄字符 + VS16」算成一列（终端画的是两列）、把家庭 emoji 算成六列，
截断时还可能把修饰符和它的基字符切成两半 —— 终端于是画出半个字符，行列全乱。

截断也按这个宽度走：pyte 建模时逐字符查表，VS16 那类组合在模型里比终端窄 ——
按真宽度裁到自己算的列数，物理终端就不会被顶出软折行。

模糊宽度（East Asian Ambiguous）是唯一没有统一答案的部分：Unicode 把决定权
留给终端，同一段文本在不同终端上宽度不同。:func:`conservative_char_width`
按两列算，对覆盖层这类**按行号绝对定位**的排版是安全的选择。
"""

from __future__ import annotations

import re
import unicodedata
from typing import Callable, Iterator

from wcwidth import wcwidth, wcswidth

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

_ZWJ = "\u200d"
_RI_FIRST = "\U0001f1e6"
_RI_LAST = "\U0001f1ff"


def char_width(cluster: str) -> int:
    """一个字符（或一整个簇）占几列。

    ``wcswidth`` 对含控制字符的串返回 -1；那些字符不占列，按零列算。
    """
    return max(wcswidth(cluster), 0)


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
    """控制字符（ESC、BEL 之类）：wcwidth 说它们零列，但它们不是修饰符。"""
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
            joins = not _is_control(char) and wcwidth(char) == 0
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
