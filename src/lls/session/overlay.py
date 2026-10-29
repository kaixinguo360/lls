"""覆盖层：浮在子进程画面之上的那一层，自己也有一个屏幕模型。

提示行、消息框、翻看器都是覆盖层。它不是「往终端上乱写几行」，而是和子进程
一样的模型：一段 ANSI 文本喂进自己的 ``PyteScreenModel``，于是颜色、宽字符、
折行、光标位置全由 pyte 算，引擎只需要知道两件事 —— 它从哪一行开始
（``anchor``）、占了几行（``lines``）。

渲染时把两者合成（见 ``Engine.render_from_model``）：覆盖层占的那几行整行
取自覆盖层模型，其余行取自底部模型。物理光标也听覆盖层的：它有自己的插入
点时用它的，没有（消息框、翻看器）就用底部模型的光标。

不写底部模型 —— 覆盖层是浮层，不是子进程的画面。
"""

from __future__ import annotations

from lls.term.model import Cell, Cursor
from lls.term.pyte_adapter import PyteScreenModel
from lls.ui.renderer import overlay_anchor

__all__ = ["Overlay"]


class Overlay:
    """一个浮层的内容、几何与光标。"""

    def __init__(self, rows: int, columns: int) -> None:
        self._rows = rows
        self._columns = columns
        self._model = PyteScreenModel(rows=rows, columns=columns, history=0)
        self._text: str | None = None
        self._base_row = 0
        # 插入点在原文里的下标；None 表示这层不占光标。
        self._cursor_index: int | None = None
        # 插入点相对覆盖层左上角的位置（由 pyte 按当前宽度算出）。
        self._cursor: Cursor | None = None
        self.anchor: int | None = None
        self.lines = 0

    # ---- 只读 ----

    @property
    def visible(self) -> bool:
        return self.anchor is not None and self.lines > 0

    @property
    def text(self) -> str | None:
        """最近画上去的原文：窗口改尺寸时按新宽度重喂一遍。"""
        return self._text

    @property
    def owns_cursor(self) -> bool:
        """这一层是否带着自己的插入点（提示行带，消息框、翻看器不带）。"""
        return self._cursor is not None

    def row_cells(self, row: int) -> tuple[Cell, ...]:
        """覆盖层第 ``row`` 行（0-based，相对它自己的左上角）。"""
        if not 0 <= row < self._rows:
            return ()
        return tuple(self._model.row_cells(row))

    def cursor(self) -> Cursor | None:
        """把物理光标放到哪；这层不占光标时返回 None。"""
        if not self.visible or self._cursor is None:
            return None
        return Cursor(x=self._cursor.x, y=self.anchor + self._cursor.y)

    # ---- 变更 ----

    def set(self, text: str, cursor: int | None = None, base_row: int = 0) -> None:
        """换一屏内容。``cursor`` 是 ``text`` 里的字符下标（转义序列也计入）。"""
        self._text = text
        self._base_row = base_row
        self._cursor_index = cursor
        self._model.reset()
        if cursor is None:
            self._cursor = None
            self._feed(text)
        else:
            # 先喂到插入点：pyte 给出的光标位置就是插入点的行列。
            self._feed(text[:cursor])
            c = self._model.cursor
            # 正好写满一行时 pyte 停在「待折行」状态，光标列会越界，夹回来。
            self._cursor = Cursor(x=min(c.x, self._columns - 1), y=c.y)
            self._feed(text[cursor:])
        self.lines = min(self.used_lines(), self._rows)
        self.anchor = overlay_anchor(base_row, self.lines, self._rows)

    def move_to(self, base_row: int) -> int | None:
        """把锚点移到 ``base_row`` 附近，返回旧锚点。

        纯展示的覆盖层（消息框、进度行）贴着子进程当前所在的行：子进程往下
        写了，它也跟下去。提示行不动 —— 它的位置由进入编辑时那一行决定。
        """
        old = self.anchor
        self._base_row = base_row
        self.anchor = overlay_anchor(base_row, self.lines, self._rows)
        return old

    def resize(self, rows: int, columns: int) -> None:
        """按新尺寸重排：内容原样重喂一遍，折行与光标由 pyte 重算。"""
        if (rows, columns) == (self._rows, self._columns):
            return
        self._rows = rows
        self._columns = columns
        self._model.resize(rows, columns)
        if self._text is not None:
            self.set(self._text, self._cursor_index, self._base_row)
        else:
            self.anchor = None
            self.lines = 0

    def clear(self) -> None:
        """收起：内容与几何一起忘掉，改尺寸时不会再画回来。"""
        self._text = None
        self._cursor_index = None
        self._cursor = None
        self.anchor = None
        self.lines = 0
        self._model.reset()

    # ---- 内部 ----

    def _feed(self, text: str) -> None:
        if text:
            self._model.feed(text.replace("\r\n", "\n").replace("\n", "\r\n").encode())

    def used_lines(self) -> int:
        """最后一处非空白行之后的行数（末尾空行不算占位）。"""
        for row in range(self._rows - 1, -1, -1):
            if any(not cell.is_blank for cell in self._model.row_cells(row)):
                return row + 1
        return 1
