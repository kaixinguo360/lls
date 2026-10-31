"""输入行补全的纯逻辑：候选与语法提示。

与 redis-cli 的 linenoise 补全同型：候选描述的是**整行**（替换光标所在的
那个词，其余原样保留），预览轮盘、响铃、Esc 取消与着色都由引擎负责 ——
本模块只回答「按 Tab 会给什么、行尾该提示什么」。

不碰终端、不读配置、不发请求：同样的输入永远得到同样的结果。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Tuple

from lls.ui import cluster_end, conservative_char_width

__all__ = [
    "Completion",
    "Completer",
    "clip_hint",
    "display_width",
    "token_span",
]


@dataclass(frozen=True)
class Completion:
    """一次补全的结果。

    ``items`` 是整行候选，按轮盘顺序排列（预览时原样顶替输入行）；
    ``hint`` 是行尾的语法提示，单行纯文本 —— 颜色、留白与截断由引擎定。
    """

    items: Tuple[str, ...] = ()
    hint: str = ""


#: 输入行补全器：``(当前文本, 光标) -> Completion``。光标是字符下标。
Completer = Callable[[str, int], Completion]


def token_span(text: str, cursor: int) -> Tuple[int, int]:
    """光标所在词的 ``[start, stop)``：以空白为界，光标可以停在词中。"""
    start = cursor
    while start > 0 and not text[start - 1].isspace():
        start -= 1
    stop = cursor
    while stop < len(text) and not text[stop].isspace():
        stop += 1
    return start, stop


def display_width(text: str) -> int:
    """按排版口径量一行的列数（模糊宽度算两列，宁可少放字）。"""
    width = 0
    index = 0
    while index < len(text):
        stop = cluster_end(text, index)
        width += conservative_char_width(text[index:stop])
        index = stop
    return width


def clip_hint(hint: str, width: int) -> str:
    """把提示截到 ``width`` 列以内；一个簇都不切开，放不下就返回空串。"""
    hint = " ".join(hint.split())  # 行尾提示只许单行
    if not hint or width <= 0:
        return ""
    out = []
    used = 0
    index = 0
    while index < len(hint):
        stop = cluster_end(hint, index)
        piece = hint[index:stop]
        cells = conservative_char_width(piece)
        if used + cells > width:
            break
        out.append(piece)
        used += cells
        index = stop
    return "".join(out)