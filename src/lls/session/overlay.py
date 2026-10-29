"""覆盖层：浮在子进程画面之上的那一层，自己也有一个屏幕模型。

提示行、消息框、翻看器都是覆盖层。它不是「往终端上乱写几行」，而是和子进程
一样的模型：一段 ANSI 文本喂进自己的 ``PyteScreenModel``，于是颜色、宽字符、
折行、光标位置全由 pyte 算，引擎只需要知道两件事 —— 它从哪一行开始
（``anchor``）、占了几行（``lines``）。

渲染时把两者合成（见 ``Engine.render_from_model``）：覆盖层占的那几行整行
取自覆盖层模型，其余行取自底部模型。物理光标归最上面的一层：它在，光标就
由它给；它不在，才用底部模型的光标。

提示行这种「带着插入点」的覆盖层位置不是画上去就定死：它贴着子进程当前
所在的行，子进程一挪，它就跟着挪（见 :meth:`Overlay.follow`）—— 否则用户
得先敲一个键，位置才归位。消息框、翻看器不跟：它们画在哪就在哪。

不写底部模型 —— 覆盖层是浮层，不是子进程的画面。

覆盖层也属于它画上去的那一屏：子进程切进备用屏（vim、less 这类全屏程序）
时它会被收走（见 ``Engine._note_screen_switch``），否则那一行会被合成帧
一直补在新画面上，退出全屏后还留着。
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
        # 插入点在原文里的下标（省略时取文本末尾）。
        self._cursor_index = 0
        # 插入点相对覆盖层左上角的位置（由 pyte 按当前宽度算出）。
        self._cursor: Cursor | None = None
        self.anchor: int | None = None
        self.lines = 0
        # 带插入点的覆盖层（提示行）跟着子进程光标走；纯展示的不跟。
        self._interactive = False

    # ---- 只读 ----

    @property
    def visible(self) -> bool:
        return self.anchor is not None and self.lines > 0

    @property
    def text(self) -> str | None:
        """最近画上去的原文：窗口改尺寸时按新宽度重喂一遍。"""
        return self._text


    def row_cells(self, row: int) -> tuple[Cell, ...]:
        """覆盖层第 ``row`` 行（0-based，相对它自己的左上角）。"""
        if not 0 <= row < self._rows:
            return ()
        return tuple(self._model.row_cells(row))

    def cursor(self) -> Cursor | None:
        """把物理光标放到哪。浮层是画面最上面的一层，光标归它。"""
        if not self.visible or self._cursor is None:
            return None
        return Cursor(x=self._cursor.x, y=min(self.anchor + self._cursor.y, self._rows - 1))

    # ---- 变更 ----

    def set(self, text: str, cursor: int | None = None, base_row: int = 0) -> None:
        """换一屏内容。

        ``cursor`` 是 ``text`` 里的字符下标（转义序列也计入）；省略就是文本
        末尾 —— 消息框、翻看器不指位置，光标落在它们写完的地方。
        """
        self._text = text
        self._base_row = base_row
        self._interactive = cursor is not None
        self._cursor_index = len(text) if cursor is None else cursor
        self._model.reset()
        # 先喂到插入点：pyte 给出的光标位置就是插入点的行列。
        self._feed(text[:self._cursor_index])
        c = self._model.cursor
        # 正好写满一行时 pyte 停在「待折行」状态，光标列会越界，夹回来。
        self._cursor = Cursor(x=min(c.x, self._columns - 1), y=c.y)
        self._feed(text[self._cursor_index:])
        self.lines = min(self.used_lines(), self._rows)
        self.anchor = overlay_anchor(base_row, self.lines, self._rows)


    def follow(self, base_row: int) -> int | None:
        """跟着子进程的光标走：锚点按 ``base_row`` 重算，返回旧锚点。

        覆盖层占的正是子进程当前所在的那一行。子进程一写东西光标就下移（或
        被 CUP 挪走），锚点必须跟着挪 —— 否则提示符留在旧行上，新写的内容
        从覆盖层底下钻出来，看着就是「位置不对」，要等用户敲一个键、覆盖层
        重画一次才归位。

        纯展示的覆盖层（消息框、翻看器）不跟：子进程的输出从它们下方走过去，
        它们不能被顶走。只有带着插入点的提示行才跟 —— 用户就打在那一行上。

        锚点没变（或这层不跟）就返回 None，调用方据此决定要不要重铺旧行。
        """
        if not self._interactive:
            return None
        old = self.anchor
        self._base_row = base_row
        self.anchor = overlay_anchor(base_row, self.lines, self._rows)
        return None if old == self.anchor else old

    def resize(self, rows: int, columns: int) -> None:
        """按新尺寸重排：内容原样重喂一遍，折行与光标由 pyte 重算。"""
        if (rows, columns) == (self._rows, self._columns):
            return
        self._rows = rows
        self._columns = columns
        self._model.resize(rows, columns)
        if self._text is not None:
            interactive = self._interactive
            self.set(self._text, self._cursor_index if interactive else None, self._base_row)
            self._interactive = interactive
        else:
            self.anchor = None
            self.lines = 0

    def clear(self) -> None:
        """收起：内容与几何一起忘掉，改尺寸时不会再画回来。"""
        self._text = None
        self._cursor_index = 0
        self._cursor = None
        self.anchor = None
        self.lines = 0
        self._interactive = False
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
