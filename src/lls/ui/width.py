"""字符在终端里占几列。

终端按列排版：宽字符（CJK、全角标点）占两列，组合符、变体选择符、ZWJ 占零
列。宽度不抄第三方包的私有表，也不自己维护码点区间：直接问
:func:`unicodedata.east_asian_width` —— 答案跟着 Python 自带的 Unicode 版本
走，不会因为某个包升了版本就悄悄换一套。

**宽度只用来防溢出，不用来定位光标。** 一个字占几列最终还是终端说了算：模糊
宽度（East Asian Ambiguous）的 ``…`` ``·`` ``→`` ``“”`` ``①`` ``±`` 在
xterm 系里是一列、在不少中日文终端里是两列，字体也参与其中；而我们既看不见
终端，也不该去测它 —— 探针要往屏幕上写字再查光标，在非 tty、多路复用、
慢终端上都会失手，查到的还可能是中间层。

于是分两处：排版预算按**宽**算（模糊宽度算两列，宁可少放字），保证画出去的
一行绝不超过列宽；光标位置则不用任何列号 —— 见
:meth:`lls.session.overlay.Overlay.cursor_position`：回到行首把这一行重写一
遍，让终端自己把光标推进到插入点，它怎么算都错不了。

排版的单位是**簇**（grapheme cluster），不是字符：基字符连同其后的零宽修饰
（组合符、VS16、ZWJ 后面的那个字符、成对的区域指示符）算一个整体。逐字符相
加会把「窄字符 + VS16」算成一列（终端画的是两列）、把家庭 emoji 算成六列，
截断时还可能把修饰符和它的基字符切成两半 —— 终端于是画出半个字符，行列全乱。
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache
from typing import Callable, Iterator

__all__ = [
    "char_width",
    "cluster_end",
    "clusters",
    "conservative_char_width",
    "fit_width",
    "strip_sgr",
    "text_width",
]

# 覆盖层里允许出现 SGR（加粗、颜色）序列：它们不占列，量宽度时必须先去掉。
_SGR = re.compile("\x1b\\[[0-9;]*m")

Measure = Callable[[str], int]

_ZWJ = "\u200d"
_RI_FIRST = "\U0001f1e6"
_RI_LAST = "\U0001f1ff"
_VS16 = "\ufe0f"
_MODIFIER_FIRST = "\U0001f3fb"
_MODIFIER_LAST = "\U0001f3ff"

# 零列的类别：组合符、包围符、格式符（含 VS16、ZWJ）、控制符。
_ZERO_CATEGORIES = ("Mn", "Me", "Cf", "Cc")

# 占两列的 EAW 取值。W/F 是所有终端都一致的两列；A（模糊）取两列是**排版**
# 口径 —— 少放一个字最多显得保守，多放一个字就会溢出、折行、把下一行顶掉。
_TWO_COLUMN_EAW = ("W", "F", "A")


@lru_cache(maxsize=None)
def _codepoint_width(char: str) -> int:
    """一个码点按排版口径占几列。"""
    if _MODIFIER_FIRST <= char <= _MODIFIER_LAST:
        # 肤色修饰：并进基字符，自己不占位。
        return 0
    if _RI_FIRST <= char <= _RI_LAST:
        # 区域指示符：成对是一个国旗（两列），落单时终端也画成两列的方框字。
        return 2
    if unicodedata.category(char) in _ZERO_CATEGORIES:
        return 0
    return 2 if unicodedata.east_asian_width(char) in _TWO_COLUMN_EAW else 1


def _is_zero(char: str) -> bool:
    """这个字符自己占零列（与它是否并进上一簇无关，见 :func:`_is_control`）。"""
    return _codepoint_width(char) == 0


def char_width(cluster: str) -> int:
    """一个字符（或一整个簇）按排版口径占几列。

    簇宽取第一个真正占列的字符：ZWJ 序列与基字符加修饰都以前者为准。带 VS16
    的簇按两列 —— 那是 emoji 呈现形式，终端画两列。
    """
    width = 0
    for char in cluster:
        char_cells = _codepoint_width(char)
        if not char_cells:
            continue
        if not width:
            width = char_cells
    if _VS16 in cluster:
        width = max(width, 2)
    return width


#: 排版口径的宽度（模糊宽度按两列）。名字留给「防溢出」一侧的调用方看：
#: 光标定位不读宽度，见模块说明。
conservative_char_width = char_width


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
    """字符串按排版口径的宽度（SGR 序列按零列算）。

    用于「一行最多放多少」这类预算，**不要**拿它算光标该在哪一列。
    """
    return sum(measure(cluster) for cluster in clusters(strip_sgr(text)))


#: :func:`text_width` 的别名：读代码时一眼看出这一处是「防溢出」口径。
fit_width = text_width
