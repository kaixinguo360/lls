"""把屏幕模型画到宿主终端。

只画模型里的行、样式和光标；模型记录的子进程标题、剪贴板、光标形状不会
发给宿主 —— 回放一段录制不应该改写播放者的剪贴板或窗口标题。
"""

from __future__ import annotations

from typing import BinaryIO, Callable, Protocol, Sequence, Tuple

from .screen import ReplayScreen

__all__ = ["CellLike", "ScreenPainter"]

_SGR_RESET = "\x1b[0m"
_DEFAULT_STYLE = ("default", "default", False, False, False, False, False)

_FG_NAMED = {
    "black": "30",
    "red": "31",
    "green": "32",
    "brown": "33",
    "blue": "34",
    "magenta": "35",
    "cyan": "36",
    "white": "37",
    "brightblack": "90",
    "brightred": "91",
    "brightgreen": "92",
    "brightbrown": "93",
    "brightblue": "94",
    "brightmagenta": "95",
    "brightcyan": "96",
    "brightwhite": "97",
}
_BG_NAMED = {
    "black": "40",
    "red": "41",
    "green": "42",
    "brown": "43",
    "blue": "44",
    "magenta": "45",
    "cyan": "46",
    "white": "47",
    "brightblack": "100",
    "brightred": "101",
    "brightgreen": "102",
    "brightbrown": "103",
    "brightblue": "104",
    "brightmagenta": "105",
    "brightcyan": "106",
    "brightwhite": "107",
}


class CellLike(Protocol):
    data: str
    style: tuple[str, str, bool, bool, bool, bool, bool]


RowRenderer = Callable[[Sequence[CellLike]], str]
Viewport = Callable[[], Tuple[int, int]]


def _color_codes(color: str, foreground: bool) -> list[str]:
    if color == "default":
        return []
    named = _FG_NAMED if foreground else _BG_NAMED
    if color in named:
        return [named[color]]
    if len(color) == 6 and all(char in "0123456789abcdef" for char in color):
        base = "38" if foreground else "48"
        red, green, blue = int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)
        return [f"{base};2;{red};{green};{blue}"]
    return []


def _style_sgr(style: tuple[str, str, bool, bool, bool, bool, bool]) -> str:
    foreground, background, bold, italics, underscore, reverse, strikethrough = style
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
    codes.extend(_color_codes(foreground, True))
    codes.extend(_color_codes(background, False))
    return "\x1b[" + ";".join(codes) + "m" if codes else ""


def _default_row_renderer(cells: Sequence[CellLike]) -> str:
    visible = [cell for cell in cells if cell.data != ""]
    while visible and not visible[-1].data.strip():
        visible.pop()
    parts: list[str] = []
    current = None
    for cell in visible:
        if cell.style != current:
            parts.append(_SGR_RESET)
            if cell.style != _DEFAULT_STYLE:
                parts.append(_style_sgr(cell.style))
            current = cell.style
        parts.append(cell.data)
    if parts:
        parts.append(_SGR_RESET)
    return "".join(parts)


class ScreenPainter:
    """按 dirty 行差量重画；``viewport`` 决定裁到宿主多大。"""

    def __init__(
        self,
        out: BinaryIO,
        *,
        row_renderer: RowRenderer | None = None,
        viewport: Viewport | None = None,
    ) -> None:
        self._out = out
        self._row_renderer = row_renderer or _default_row_renderer
        self._viewport = viewport

    def paint(self, screen: ReplayScreen, *, full: bool = False) -> None:
        rows, columns = self._limits(screen)
        if rows <= 0 or columns <= 0:
            return
        if full:
            dirty = range(rows)
            self._out.write(b"\x1b[H\x1b[2J")
        else:
            dirty = sorted(set(screen.dirty) & set(range(rows)))
        written: list[str] = []
        for row in dirty:
            written.append(f"\x1b[{row + 1};1H")
            written.append(self._row_renderer(screen.row_cells(row)[:columns]))
            written.append("\x1b[K")
        cursor = screen.cursor
        if cursor.hidden:
            written.append("\x1b[?25l")
        else:
            written.append("\x1b[?25h")
            if 0 <= cursor.x < columns and 0 <= cursor.y < rows:
                written.append(f"\x1b[{cursor.y + 1};{cursor.x + 1}H")
        if written:
            self._out.write("".join(written).encode("utf-8"))
        screen.clear_dirty()
        self._out.flush()

    def _limits(self, screen: ReplayScreen) -> tuple[int, int]:
        if self._viewport is None:
            return screen.rows, screen.columns
        rows, columns = self._viewport()
        return min(rows, screen.rows), min(columns, screen.columns)
