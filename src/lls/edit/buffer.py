"""单行编辑器：文本缓冲、光标、历史。

编辑语义按 readline 来：

* 光标左右移动、Home/End；
* 退格（删光标前）、删除（删光标处）；
* 历史上下翻：翻页前保存草稿，翻回末尾恢复草稿；
* 历史可以分类：给了 ``history_kind`` 就只翻与当前这行同类的那几条；
* 历史去重：连续相同的内容只留一份；
* 光标按**簇**移动：退格、左右、删除都不切开一个 emoji 或组合字符。

本模块只维护文本状态，不画终端，也**不量宽度**：一个字占几列由终端决定，
光标该在哪一列不由这里算（见 :mod:`lls.ui.width` 与
:meth:`lls.session.overlay.Overlay.cursor_position`）。
"""

from __future__ import annotations

from typing import Callable

from lls.ui import cluster_end

__all__ = ["LineEditor"]


class LineEditor:
    """单行编辑器，带历史。"""

    def __init__(
        self,
        history: list[str] | None = None,
        *,
        history_kind: Callable[[str], object] | None = None,
    ) -> None:
        self._history: list[str] = list(history or [])
        # ↑ 只走同一类的旧行：分类由调用方给（任务与 /命令各走各的，见
        # agent 的 _history_kind）。没给就是只有一类，行为和以前一样。
        self.history_kind = history_kind
        self._text = ""
        self._cursor = 0
        self._hist_idx: int | None = None  # None = 正在编辑新行
        self._hist_kind: object = ""        # 正在翻的那一类
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

    def set_text(self, value: str, cursor: int | None = None) -> None:
        """整体换掉这一行（补全落地用）；默认光标到末尾。

        不动 ``_hist_idx`` / ``_draft``：从历史里召回的一行被补全后，↓ 仍按
        readline 的老规矩回到草稿，不会因为改了字就丢历史位置。
        """
        self._text = value
        self._cursor = len(value) if cursor is None else max(0, min(cursor, len(value)))

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
        # 翻哪一类，看这一趟是从哪儿出发的：从草稿出发就按草稿分类（敲了
        # ``/`` 才翻命令），已经在历史里就一路翻同一类，不会中途跳到另一类。
        kind = self._hist_kind if self._hist_idx is not None else self._kind(self._text)
        start = len(self._history) if self._hist_idx is None else self._hist_idx
        for index in range(start - 1, -1, -1):
            if self._kind(self._history[index]) != kind:
                continue  # 另一类的旧行：跳过，↑ 不该把 /help 翻进任务里
            if self._hist_idx is None:
                self._draft = self._text
            self._hist_kind = kind
            self._hist_idx = index
            self._text = self._history[index]
            self._cursor = len(self._text)
            return
        # 没有更早的同类：原地不动，草稿也不动。

    def history_down(self) -> None:
        if self._hist_idx is None:
            return
        for index in range(self._hist_idx + 1, len(self._history)):
            if self._kind(self._history[index]) != self._hist_kind:
                continue
            self._hist_idx = index
            self._text = self._history[index]
            self._cursor = len(self._text)
            return
        # 没有更晚的同类：翻回末尾，恢复草稿
        self._hist_idx = None
        self._text = self._draft
        self._cursor = len(self._text)

    def _kind(self, value: str) -> object:
        """这一行属于哪一类历史：没给分类函数时只有一类。"""
        return self.history_kind(value) if self.history_kind is not None else ""

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
