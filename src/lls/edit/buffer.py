"""单行编辑器：文本缓冲、光标、历史。

编辑语义按 readline 来：

* 光标左右移动、Home/End；
* 退格（删光标前）、删除（删光标处）；
* 历史上下翻：翻页前保存草稿，翻回末尾恢复草稿；
* 历史去重：连续相同的内容只留一份；
* 光标按**簇**移动：退格、左右、删除都不切开一个 emoji 或组合字符。

本模块只维护文本状态，不画终端，也**不量宽度**：一个字占几列由终端决定，
光标该在哪一列不由这里算（见 :mod:`lls.ui.width` 与
:meth:`lls.session.overlay.Overlay.cursor_position`）。
"""

from __future__ import annotations

from lls.ui import cluster_end

__all__ = ["LineEditor"]


class LineEditor:
    """单行编辑器，带历史。"""

    def __init__(self, history: list[str] | None = None) -> None:
        self._history: list[str] = list(history or [])
        self._text = ""
        self._cursor = 0
        self._hist_idx: int | None = None  # None = 正在编辑新行
        self._draft = ""                   # 浏览历史前保存的草稿

    # ---- 只读 ----

    @property
    def text(self) -> str:
        return self._text

    @property
    def cursor(self) -> int:
        """光标位置（字符索引，非字节）。"""
        return self._cursor

    @property
    def history(self) -> list[str]:
        return self._history

    def reset(self, value: str = "") -> None:
        self._text = value
        self._cursor = len(value)
        self._hist_idx = None
        self._draft = ""

    # ---- 编辑 ----

    def insert(self, char: str) -> None:
        self._text = self._text[: self._cursor] + char + self._text[self._cursor :]
        self._cursor += 1

    def backspace(self) -> None:
        if self._cursor > 0:
            start = self._cluster_start(self._cursor)
            self._text = self._text[:start] + self._text[self._cursor :]
            self._cursor = start

    def delete(self) -> None:
        if self._cursor < len(self._text):
            stop = cluster_end(self._text, self._cursor)
            self._text = self._text[: self._cursor] + self._text[stop:]

    # ---- 光标移动 ----

    def left(self) -> None:
        if self._cursor > 0:
            self._cursor = self._cluster_start(self._cursor)

    def right(self) -> None:
        if self._cursor < len(self._text):
            self._cursor = cluster_end(self._text, self._cursor)

    def home(self) -> None:
        self._cursor = 0

    def end(self) -> None:
        self._cursor = len(self._text)

    # ---- 历史 ----

    def history_up(self) -> None:
        if not self._history:
            return
        if self._hist_idx is None:
            self._draft = self._text
            self._hist_idx = len(self._history)
        if self._hist_idx > 0:
            self._hist_idx -= 1
            self._text = self._history[self._hist_idx]
            self._cursor = len(self._text)

    def history_down(self) -> None:
        if self._hist_idx is None:
            return
        if self._hist_idx < len(self._history) - 1:
            self._hist_idx += 1
            self._text = self._history[self._hist_idx]
            self._cursor = len(self._text)
        else:
            # 翻回末尾：恢复草稿
            self._hist_idx = None
            self._text = self._draft
            self._cursor = len(self._text)

    def _cluster_start(self, index: int) -> int:
        """``index`` 所在簇的起点：光标停在这里，不会落进一个字符的中间。"""
        start = 0
        while start < index:
            stop = cluster_end(self._text, start)
            if index <= stop:
                return start
            start = stop
        return start

    # ---- 提交 ----

    def submit(self, *, keep: bool = True) -> str:
        """提交当前行：返回内容，默认加入历史（连续相同去重）。

        ``keep`` 为 False 时不进历史 —— 供「只是选一个键」的提示使用，
        这类输入事后没有召回价值，留在历史里只会污染 ↑。
        """
        text = self._text
        if keep:
            self.record(text)
        return text

    def record(self, value: str) -> None:
        """把一条内容记入历史（不改变当前编辑状态），连续相同去重。"""
        if value and (not self._history or self._history[-1] != value):
            self._history.append(value)
