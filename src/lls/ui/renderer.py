"""显示原语：文本换行、覆盖层定位、按模型渲染。

分两类：

* **纯计算**：``wrap_multi_lines`` / ``overlay_anchor`` 与 :mod:`lls.ui.width` ——
  给定文本与终端宽度，算出该占几行、覆盖层该从哪一行开始，不碰终端；
* **写终端**：``render_row_cells``（把模型的单元格编码成带样式的字节）。

覆盖层的几何约束由 ``overlay_anchor`` 统一负责：放不下就整体上移，绝不越过
屏幕底部 —— 一旦越界终端会滚动，物理行与模型行随即错位。
"""

from __future__ import annotations

from .width import (
    Measure,
    char_width,
    cluster_end,
    conservative_char_width,
)

SGR_RESET = "\x1b[0m"

__all__ = [
    "SGR_RESET",
    "char_width",
    "conservative_char_width",
    "overlay_anchor",
    "render_row_cells",
    "wrap_multi_lines",
]


def _csi_end(text: str, start: int) -> int:
    """``text[start]`` 起是一个 ESC ``[`` 序列：返回它结束后的下标。"""
    index = start + 2
    while index < len(text) and not ("@" <= text[index] <= "~"):
        index += 1
    return min(index + 1, len(text))


def _next_style(style: str, seq: str) -> str:
    """按一条 SGR 序列更新「当前仍生效的样式」。

    换行后要把样式重放一次，否则新行从终端残留的状态开始 —— 屏幕
是分多次
    写出来的，不能指望终端帮我们记住上一行末的状态。
    """
    if not seq.endswith("m"):
        return style
    params = seq[2:-1]
    if params in ("", "0"):
        return ""
    if "0" in params.split(";"):
        # 「重置 + 又设了新样式」（如 ``0;1``）：之前的样式全丢。
        return seq
    return style + seq


def wrap_multi_lines(
    display: str,
    width: int,
    end: str = "\r\n",
    measure: Measure = char_width,
) -> tuple[str, int]:
    """将长字符串按终端宽度自动换行。返回 (输出串, 行数)。

    ``measure`` 决定一个簇算几列；覆盖层排版传入
    :func:`conservative_char_width`，避免在模糊宽度上窄算。

    切分单位是簇而不是字符：组合符、VS16、ZWJ 序列必须和基字符留在同一行，
    切开就等于让终端画出半个字符（宽度算得再准也没用）。

    SGR（颜色/加粗）转义序列按零列算，并跨行重放：每行结尾先重置、
    下一行开头再补回生效中的样式。这样按行绝对定位重画时，每行都
    自带完整样式，不依赖画行的先后顺序。
    """
    output = ""
    lines = 1
    n = 0
    style = ""
    index = 0
    size = len(display)
    while index < size:
        char = display[index]
        if char == "\x1b" and display[index + 1 : index + 2] == "[":
            stop = _csi_end(display, index)
            seq = display[index:stop]
            output += seq
            style = _next_style(style, seq)
            index = stop
            continue
        if char == "\n":
            output += (SGR_RESET if style else "") + end + style
            lines += 1
            n = 0
            index += 1
            continue
        stop = cluster_end(display, index)
        cluster = display[index:stop]
        index = stop
        cluster_width = measure(cluster)
        if n + cluster_width > width:
            output += (SGR_RESET if style else "") + end + style
            lines += 1
            n = 0
        output += cluster
        n += cluster_width
    if style:
        output += SGR_RESET
    return output, lines


def overlay_anchor(cursor_y: int, lines_needed: int, rows: int) -> int:
    """覆盖层的起始行：贴近模型光标，但保证整体不越过屏幕底部。

    覆盖层（命令行、消息）是浮在画面之上的临时显示。它**必须自己保证不越界**：
    一旦它画到屏幕底部之外，终端就会滚动，把上方内容顶出去 —— 物理画面因此
    与屏幕模型错位，之后子进程的回显会被写进错误的位置。

    规则：

    * 空间够：从模型光标所在行开始画（直接覆盖光标行）；
    * 空间不够：整体上移，让末行对齐屏幕底（``rows - lines_needed``）；
    * 覆盖层比屏幕还高：贴顶（0），多余部分无法完整显示。

    返回 0-based 行号。
    """
    if lines_needed >= rows:
        return 0
    return max(0, min(cursor_y, rows - lines_needed))


# ---- 按屏幕模型渲染（模型是画面的唯一来源）----
#
# 渲染一律从模型取内容，不重放原始字节：原始字节会被截断（可能断在转义序列
# 中间）、且包含进入全屏应用之前的全部历史，重放等于重演一遍。
# 从状态重绘则结果确定、无截断问题、可精确还原颜色 —— 与 tmux/screen 等
# 终端多路复用器的做法一致。

_FG_NAMED = {
    "black": "30", "red": "31", "green": "32", "brown": "33",
    "blue": "34", "magenta": "35", "cyan": "36", "white": "37",
    "brightblack": "90", "brightred": "91", "brightgreen": "92",
    "brightbrown": "93", "brightblue": "94", "brightmagenta": "95",
    "brightcyan": "96", "brightwhite": "97",
}
_BG_NAMED = {
    "black": "40", "red": "41", "green": "42", "brown": "43",
    "blue": "44", "magenta": "45", "cyan": "46", "white": "47",
    "brightblack": "100", "brightred": "101", "brightgreen": "102",
    "brightbrown": "103", "brightblue": "104", "brightmagenta": "105",
    "brightcyan": "106", "brightwhite": "107",
}
_DEFAULT_STYLE = ("default", "default", False, False, False, False, False)


def _color_codes(color: str, fg: bool) -> list[str]:
    if color == "default":
        return []
    named = _FG_NAMED if fg else _BG_NAMED
    if color in named:
        return [named[color]]
    # 256 色 / 24bit：pyte 用 'rrggbb' 十六进制表示
    if len(color) == 6 and all(c in "0123456789abcdef" for c in color):
        base = "38" if fg else "48"
        r, g, b = int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)
        return [f"{base};2;{r};{g};{b}"]
    return []


def _style_sgr(style: tuple) -> str:
    """把 Cell.style 转成 SGR 序列。"""
    fg, bg, bold, italics, underscore, reverse, strikethrough = style
    codes: list[str] = []
    if bold:
        codes.append("1")
    if italics:
        codes.append("3")
    if underscore:
        codes.append("4")
    if reverse:
        codes.append("7")
    if strikethrough:
        codes.append("9")
    codes.extend(_color_codes(fg, True))
    codes.extend(_color_codes(bg, False))
    return "\x1b[" + ";".join(codes) + "m" if codes else ""


def render_row_cells(cells, keep_trailing_blank: bool = False) -> str:
    """把一行 Cell 渲染成带颜色的字符串（跳过宽字符右半格占位，去尾部空白）。

    ``keep_trailing_blank`` 为真时尾部空白照写：光标定位要把「插入点之前这一
    行」重写一遍，靠终端自己把光标推到插入点，行尾的空格也是推进量的一部分。
    整行重画时用不上它 —— 那里空白由 ``EL`` 擦掉更省字节。
    """
    runs: list[tuple[tuple, list[str]]] = []
    prev: tuple | None = None
    buf: list[str] = []
    for c in cells:
        if c.data == "":      # 宽字符右半格占位
            continue
        if c.style != prev:
            if buf:
                runs.append((prev, buf))
                buf = []
            prev = c.style
        buf.append(c.data)
    if buf:
        runs.append((prev, buf))

    # 去掉尾部空白（视觉等价，避免输出大量空格）
    if not keep_trailing_blank:
        while runs and not "".join(runs[-1][1]).strip():
            runs.pop()

    parts: list[str] = []
    for st, tx in runs:
        text = "".join(tx)
        if st is not None and st != _DEFAULT_STYLE:
            parts.append(_style_sgr(st))
            parts.append(text)
            parts.append("\x1b[0m")
        else:
            parts.append(text)
    return "".join(parts)

