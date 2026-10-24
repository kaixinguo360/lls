"""终端屏幕模型：与具体实现无关的接口契约。

这一层存在的唯一目的，是把「ANSI 字节流 → 可读取的屏幕状态」变成一个可以替换、
可以单测、可以对拍的边界。

约定（写进 CONTRIBUTING，review 时按此卡）：

* 本模块是纯数据定义，不 import ``os`` / ``sys``，不做任何 I/O。
* 实现方（pyte 适配器 / 自研 VT 内核 / 测试替身）只通过本模块的类型交互。
* 行号、列号一律 0-based；``rows`` 指可见屏幕高度，不含回滚区。
* ``feed()`` 接受**任意切分**的字节流，分块方式不得影响最终状态
  （由 tests/golden 的分块不变性测试守护）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import FrozenSet, Protocol, Sequence, runtime_checkable

__all__ = [
    "BLANK",
    "Cell",
    "Cursor",
    "DirtyRows",
    "ScreenModel",
]


# 这是赋值而非注解，``from __future__ import annotations`` 不覆盖它；
# ``frozenset[int]`` 要 3.9 才可下标，故用 typing 的形式。
DirtyRows = FrozenSet[int]
"""自上次 ``clear_dirty()`` 以来内容发生变化的可见行号。"""


@dataclass(frozen=True)
class Cell:
    """一个字符格的最终显示状态。

    ``data`` 为空串表示该格被左侧的宽字符（如中文、emoji）占用了右半边，
    渲染时应当跳过而不是再画一个空格。
    """

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
        """可哈希的样式签名，供差量渲染比较。"""
        return (
            self.fg,
            self.bg,
            self.bold,
            self.italics,
            self.underscore,
            self.reverse,
            self.strikethrough,
        )

    @property
    def is_blank(self) -> bool:
        """内容为空白且没有任何样式（渲染层据此整段跳过）。"""
        return self.data in (" ", "") and self.style == BLANK_STYLE


BLANK_STYLE = ("default", "default", False, False, False, False, False)
BLANK = Cell()


@dataclass(frozen=True)
class Cursor:
    """光标位置与可见性。"""

    x: int = 0
    y: int = 0
    hidden: bool = False


@runtime_checkable
class ScreenModel(Protocol):
    """终端屏幕模型的协议。

    实现者需要满足两条不变量：

    1. ``feed()`` 可以任意切分调用而结果一致；
    2. ``dirty`` 只增不减，直到调用 ``clear_dirty()``。
    """

    @property
    def rows(self) -> int:
        """可见屏幕行数。"""
        ...

    @property
    def columns(self) -> int:
        """可见屏幕列数。"""
        ...

    @property
    def cursor(self) -> Cursor:
        """当前光标。"""
        ...

    @property
    def alternate(self) -> bool:
        """当前是否位于备用缓冲区（``?1049`` 等）。"""
        ...

    @property
    def dirty(self) -> DirtyRows:
        """尚未消费的变化行号。"""
        ...

    def clear_dirty(self) -> None:
        """消费并清空变化行号。"""
        ...

    def feed(self, data: bytes) -> None:
        """喂入一段（可能被任意切断的）字节流。"""
        ...

    def resize(self, rows: int, columns: int) -> None:
        """调整可见区域尺寸。"""
        ...

    def row_text(self, row: int) -> str:
        """第 ``row`` 行的纯文本，已裁掉尾部空白。"""
        ...

    def row_cells(self, row: int) -> Sequence[Cell]:
        """第 ``row`` 行的逐格状态，长度为 ``columns``。"""
        ...

    def lines(self) -> Sequence[str]:
        """可见区域的全部文本行。"""
        ...

    def scrollback(self, limit: int | None = None) -> Sequence[str]:
        """回滚区文本（最近的在最后）。"""
        ...

    def text(self, *, limit: int | None = None, include_scrollback: bool = True) -> str:
        """整屏文本。

        默认取「回滚区尾部 + 可见区」，并去掉末尾空行；``limit`` 用于按行数
        截断。
        """
        ...
