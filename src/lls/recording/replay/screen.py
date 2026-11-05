"""回放用的屏幕模型边界。

播放器只依赖 :class:`ReplayScreen`；默认实现基于 pyte，够普通 shell 录制使用。
LLS 自己会注入 ``PyteScreenModel``，从而与实时画面共用备用屏和宽度规则。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence, runtime_checkable

import pyte

__all__ = ["Cell", "Cursor", "ReplayScreen", "make_screen"]


@dataclass(frozen=True)
class Cell:
    """一个字符格的最终显示状态；与 LLS 的 ``Cell`` 字段同形。"""

    data: str = " "
    fg: str = "default"
    bg: str = "default"
    bold: bool = False
    italics: bool = False
    underscore: bool = False
    reverse: bool = False
    strikethrough: bool = False

    @property
    def style(self) -> tuple[str, str, bool, bool, bool, bool, bool]:
        return (
            self.fg,
            self.bg,
            self.bold,
            self.italics,
            self.underscore,
            self.reverse,
            self.strikethrough,
        )


@dataclass(frozen=True)
class Cursor:
    x: int
    y: int
    hidden: bool


@runtime_checkable
class ReplayScreen(Protocol):
    """播放器需要的最小屏幕接口。"""

    @property
    def rows(self) -> int: ...

    @property
    def columns(self) -> int: ...

    @property
    def cursor(self) -> Cursor: ...

    @property
    def dirty(self) -> frozenset[int]: ...

    def feed(self, data: bytes) -> None: ...

    def resize(self, rows: int, columns: int) -> None: ...

    def row_cells(self, row: int) -> Sequence[Cell]: ...

    def clear_dirty(self) -> None: ...

    def text(self) -> str: ...


_BLANK = Cell()


class _PyteScreen:
    """pyte 的薄封装：默认回放屏。

    它不做主/备缓冲切换（pyte 只记录 ``?1049`` 模式，不切换内容）；全屏程序
    的录制请注入 LLS 的 ``PyteScreenModel``。
    """

    def __init__(self, rows: int, columns: int) -> None:
        self._screen = pyte.Screen(columns, rows)
        self._stream = pyte.ByteStream(self._screen)

    @property
    def rows(self) -> int:
        return self._screen.lines

    @property
    def columns(self) -> int:
        return self._screen.columns

    @property
    def cursor(self) -> Cursor:
        value = self._screen.cursor
        return Cursor(value.x, value.y, bool(getattr(value, "hidden", False)))

    @property
    def dirty(self) -> frozenset[int]:
        return frozenset(self._screen.dirty)

    def feed(self, data: bytes) -> None:
        self._stream.feed(data)

    def resize(self, rows: int, columns: int) -> None:
        # pyte 的参数顺序是 (lines, columns)。
        self._screen.resize(rows, columns)

    def row_cells(self, row: int) -> Sequence[Cell]:
        line = self._screen.buffer.get(row, {})
        cells = []
        for column in range(self._screen.columns):
            cells.append(_to_cell(line.get(column)))
        return tuple(cells)

    def clear_dirty(self) -> None:
        self._screen.dirty.clear()

    def text(self) -> str:
        lines = [line.rstrip() for line in self._screen.display]
        while lines and not lines[-1]:
            lines.pop()
        return "\n".join(lines)


def _to_cell(char: object) -> Cell:
    if char is None:
        return _BLANK
    data = getattr(char, "data", " ")
    if data is None:
        data = ""
    if len(data) > 1:
        data = data[0]
    return Cell(
        data=data,
        fg=getattr(char, "fg", "default"),
        bg=getattr(char, "bg", "default"),
        bold=bool(getattr(char, "bold", False)),
        italics=bool(getattr(char, "italics", False)),
        underscore=bool(getattr(char, "underscore", False)),
        reverse=bool(getattr(char, "reverse", False)),
        strikethrough=bool(getattr(char, "strikethrough", False)),
    )


def make_screen(rows: int, columns: int) -> ReplayScreen:
    """默认屏幕工厂；LLS 会换成自己的 ``PyteScreenModel``。"""
    return _PyteScreen(rows, columns)
