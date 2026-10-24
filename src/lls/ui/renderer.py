"""显示原语：文本换行、覆盖层定位、按模型渲染。

分两类：

* **纯计算**：``wrap_multi_lines`` / ``char_width`` / ``overlay_anchor`` ——
  给定文本与终端宽度，算出该占几行、覆盖层该从哪一行开始，不碰终端；
* **写终端**：``LinePrinter``（覆盖层绘制）、``render_row_cells``（把模型
  单元格编码成带样式的字节）、``print_screen_perfect``（按模型整屏显示）。

覆盖层的几何约束由 ``overlay_anchor`` 统一负责：放不下就整体上移，绝不越过
屏幕底部 —— 一旦越界终端会滚动，物理行与模型行随即错位。
"""

from __future__ import annotations

from lls.tty import Console

__all__ = [
    "LinePrinter",
    "char_width",
    "content_start",
    "overlay_anchor",
    "print_screen_perfect",
    "render_row_cells",
    "wrap_multi_lines",
]

# 字符显示宽度表（legacy display.py 原样，经实战验证）。
_CHAR_WIDTHS = [
    (126, 1), (159, 0), (687, 1), (710, 0), (711, 1),
    (727, 0), (733, 1), (879, 0), (1154, 1), (1161, 0),
    (4347, 1), (4447, 2), (7467, 1), (7521, 0), (8369, 1),
    (8426, 0), (9000, 1), (9002, 2), (11021, 1), (12350, 2),
    (12351, 1), (12438, 2), (12442, 0), (19893, 2), (19967, 1),
    (55203, 2), (63743, 1), (64106, 2), (65039, 1), (65059, 0),
    (65131, 2), (65279, 1), (65376, 2), (65500, 1), (65510, 2),
    (120831, 1), (262141, 2), (1114109, 1),
]


def char_width(c: str) -> int:
    """返回字符 c 在终端的显示宽度。"""
    o = ord(c)
    if o == 0x0E or o == 0x0F:
        return 0
    for num, wid in _CHAR_WIDTHS:
        if o <= num:
            return wid
    return 1


def wrap_multi_lines(display: str, width: int, end: str = "\r\n") -> tuple[str, int]:
    """将长字符串按终端宽度自动换行。返回 (输出串, 行数)。"""
    output = ""
    lines = 1
    n = 0
    for c in display:
        if c == "\n":
            output += end
            lines += 1
            n = 0
            continue
        c_width = char_width(c)
        if n + c_width > width:
            output += end
            lines += 1
            n = 0
        output += c
        n += c_width
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


class LinePrinter:
    """封装 clear_lines / print_lines，持有 Console 与终端宽度。"""

    def __init__(self, console: Console) -> None:
        self._console = console

    def clear_lines(self, lines_all: int, lines_cur: int, clear: bool = True) -> tuple[int, int]:
        """清除多行终端输出。返回 (1, 1)（与 legacy 一致）。"""
        w = self._console
        if lines_all != lines_cur:
            for _ in range(lines_all - lines_cur):
                w.write(b"\r\x1b[1B")
        for _ in range(lines_all - 1):
            if clear:
                w.write(b"\x1b[2K")
            w.write(b"\r\x1b[1A")
        if clear:
            w.write(b"\x1b[2K")
        w.write(b"\r")
        return 1, 1

    def print_lines(
        self,
        text: str,
        cursor: int | None = None,
        width: int = 80,
        anchor_row: int | None = None,
    ) -> tuple[int, int]:
        """打印多行文本并把光标定位到 ``cursor``（字符索引）。返回 (行数, 光标行数)。

        ``anchor_row`` 给定时先绝对定位到该行行首再绘制（覆盖层用），
        否则沿用相对移动（从当前光标处向上回退）。
        """
        w = self._console
        line, lines_all = wrap_multi_lines(text, width)
        if anchor_row is not None:
            w.write(f"\x1b[{anchor_row + 1};1H".encode())
        w.write(b"\x1b[2K\r")
        w.write(line.encode())
        if cursor is not None and cursor != len(text):
            for _ in range(lines_all - 1):
                w.write(b"\r\x1b[1A")
            line_prev, lines_cur = wrap_multi_lines(text[:cursor], width)
            w.write(b"\r")
            w.write(line_prev.encode())
        else:
            lines_cur = lines_all
        return lines_all, lines_cur


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


def render_row_cells(cells) -> str:
    """把一行 Cell 渲染成带颜色的字符串（跳过宽字符右半格占位，去尾部空白）。"""
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


def content_start(screen) -> int:
    """第一个非空行的下标；全空时返回光标所在行。

    模型顶部可能有一段空行（见 ``Controller`` 启动时对物理光标位置的对齐），
    这些空行**不是**要显示的内容，重绘时不应把它们搬到屏幕顶部。
    """
    rows = screen.rows
    for row in range(rows):
        if screen.row_text(row).strip():
            return row
    return min(screen.cursor.y, rows - 1)





def print_screen_perfect(
    screen,
    console: Console,
    end: str = "\r\n",
    tail: str = "",
    width: int | None = None,
    height: int | None = None,
    frame: bool = False,
    raw: bool = False,
) -> None:
    """精确移植 legacy terminal.print_screen_perfect 的外观。

    边框 ``+----+`` + 光标反色高亮 + 状态信息行。
    修复了 legacy 里 ``y_begin`` 在 ``y_end`` 赋值前引用导致的 NameError。
    """
    if width is None:
        width = (screen.columns - 4) if frame else (screen.columns - 2)

    lines = screen.lines()
    # legacy 的 screen.lines 是**动态列表**（长度 = 光标到达的最大行 + 1），
    # 故非 frame 时不画尾部空行。pyte 的 lines() 固定 rows 行（尾部补空），
    # 这里按 legacy 语义裁到最后一个非空行，避免画出大量空行把画面推走。
    content_lines = len(lines)
    while content_lines > 1 and lines[content_lines - 1].strip() == "":
        content_lines -= 1

    if height is None:
        height = screen.rows if frame else content_lines

    # legacy bug 修复：y_end 必须先于 y_begin 计算
    y_end = content_lines if content_lines > height else height
    y_begin = content_lines - height if content_lines > height else 0

    # 跳过开头的空行：模型顶部可能因启动对齐（见 Controller 的光标对齐）留空，
    # 那些行代表「模型未建模的历史画面」，不是内容，显示视图里不应占位。
    first = 0
    while first < content_lines - 1 and lines[first].strip() == "":
        first += 1
    if first > y_begin:
        y_begin = first

    if not raw:
        console.write(("+" + "-" * width + "+" + end).encode())
    for i in range(y_begin, y_end):
        line = lines[i] if i < len(lines) else ""
        line += tail
        x_begin = 0
        if frame:
            x_end = width if len(line) > width else len(line)
        else:
            x_end = len(line)
        display = line[x_begin:x_end] + " " * (width - (x_end - x_begin))
        if screen.cursor.y == i:
            if frame:
                if screen.cursor.x >= x_begin and screen.cursor.x < x_end:
                    cursor = screen.cursor.x + x_begin
                    display = (
                        display[:cursor]
                        + "\x1b[7m" + display[cursor : cursor + 1] + "\x1b[0m"
                        + display[cursor + 1 :]
                    )
            else:
                while screen.cursor.x > len(display) - 1:
                    display += " "
                display = (
                    display[: screen.cursor.x]
                    + "\x1b[7m" + display[screen.cursor.x : screen.cursor.x + 1] + "\x1b[0m"
                    + display[screen.cursor.x + 1 :]
                )
        if frame:
            console.write(("|" + display + "|").encode())
        else:
            console.write(display.rstrip(" ").encode())
        if not (raw and i == (y_end - 1)):
            console.write(end.encode())
        console.write(b"\r")
    if not raw:
        console.write(("+" + "-" * width + "+" + end).encode())
        buffer = "alter" if screen.alternate else "main"
        console.write(
            (
                f"cursor: {{x={screen.cursor.x + 1},y={screen.cursor.y + 1}}}"
                f", lines: {content_lines}"
                f", offset: 0"
                f", height: {screen.rows}"
                f", buffer: {buffer}"
                f", mode: normal"
                + end
            ).encode()
        )
