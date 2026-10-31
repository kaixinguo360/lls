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
    "Ghost",
    "clip_hint",
    "clip_text",
    "display_width",
    "ghost_for",
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
    hint = hint.replace("\n", " ").replace("\t", " ")  # 行尾提示只许单行
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

def clip_text(text: str, width: int) -> str:
    """按列宽截断，但**保留原样的空格**。

    虚影里空格是语义的一部分（``/model `` 的尾空格、分段之间的留白），
    不能像 :func:`clip_hint` 那样先压成单行再截。
    """
    if not text or width <= 0:
        return ""
    out = []
    used = 0
    index = 0
    while index < len(text):
        stop = cluster_end(text, index)
        piece = text[index:stop]
        cells = conservative_char_width(piece)
        if used + cells > width:
            break
        out.append(piece)
        used += cells
        index = stop
    return "".join(out)


@dataclass(frozen=True)
class Ghost:
    """行尾虚影（ghost text）：接受时整行写进去，以及显示出来的两段。

    ``candidate`` 是按下接受键后编辑器里的整行；``suffix`` 是它相对当前
    输入的“剩余字符”，``usage`` 是用法里去掉命令词后的参数段。两段都画在
    光标右侧，颜色由引擎决定，本模块只管内容。
    """

    candidate: str = ""
    suffix: str = ""
    usage: str = ""


def _usage_tail(hint: str) -> str:
    """从用法里取参数段。

    补全器给的 hint 可能已经去掉了输入过的前缀（``tle [文字]``、`` [文字]``），
    也可能带着完整命令词（``/sessions [--all]``）。两种形状都认：先按空白
    切开，第一个词是命令词就丢掉它，只剩参数段（``[``、``<``、``-`` 开头）。
    """
    hint = hint.strip()
    if not hint:
        return ""
    parts = hint.split(None, 1)
    parameter = parts[0][:1] in ("[", "<", "-")
    if len(parts) == 1:
        return hint if parameter else ""
    if parameter:
        return hint
    return parts[1]


def ghost_for(text: str, cursor: int, completion: Completion) -> Ghost:
    """从补全结果里取虚影。

    只取第一个候选：虚影是“照这个补”的建议，不轮换。候选不以当前文本为
    前缀时（别名补成规范名、大小写不同），给不出剩余字符，虚影留空、只显示
    用法段。多个候选时在剩余字符后跟一个 ``…``，提示还有别的选择。
    """
    if cursor != len(text):
        return Ghost()
    usage = _usage_tail(completion.hint)
    if not completion.items:
        return Ghost(usage=usage)
    candidate = completion.items[0]
    if not candidate.startswith(text):
        return Ghost(usage=usage)
    suffix = candidate[len(text):]
    if suffix and len(completion.items) > 1:
        suffix = suffix.rstrip() + " …"
    return Ghost(candidate=candidate, suffix=suffix, usage=usage)