"""pyte 适配器：把 pyte 包装成 :class:`~lls.term.model.ScreenModel`。

pyte 的两个关键限制（本适配器必须自己解决）：

1. ``?1049/?1047/?47`` **只把模式码记进 ``screen.mode``，不切换内容** ——
   这里维护主/备两张屏，按开关序列自行切换。
2. ``pyte.Stream`` 在构造时就把 listener **绑死**进解析器（``_parser_fsm`` 里
   ``listener = self.listener``，CSI/ESC 分发表用 ``getattr(listener, attr)`` 建好），
   事后改 ``stream.listener`` 只对纯文本生效，转义序列仍发给旧屏。
   —— 因此这里为每张屏各建一个 ``ByteStream``，并且**在 feed 时按开关序列拆分
   字节块**：开关可能和后续内容在同一个块里，必须在正确的边界切换。

pyte 的行为约定（本适配器依赖这些事实，版本 0.8.x）：

* ``ByteStream`` 自带增量 UTF-8 解码，跨块安全；宽字符光标推进正确。
* ``?25l`` 已建模（``cursor.hidden``）。
* ``display`` 每行补空格到 columns —— 取文本必须 ``rstrip()``。
* ``resize(lines, columns)`` 参数顺序与构造函数相反。
* ``dirty`` 是可消费的 ``set``，``clear()`` 即清空。
* ``HistoryScreen`` 在 ``__getattribute__`` 里包裹每个事件，而 ``draw()`` 每写一个
  字符都要访问多个属性，开销接近翻倍。回滚区因此由 ``_Screen`` 自行记录。
* 私有 CSI（``ESC [ ? … <final>``）一律以 ``private=True`` 派发，而多数处理函数
  并没有这个参数 —— pyte 自身的不一致，``_tolerant`` 负责把这类序列挡在引擎
  之外，避免一个读不懂的转义序列中断整个 ``feed()``。
"""

from __future__ import annotations

from collections import deque
from typing import Any

import pyte

from lls.ui.width import conservative_char_width

from .model import BLANK, Cell, Cursor

__all__ = ["PyteScreenModel"]

# pyte 的宽度不是写死在 ``draw()`` 里，而是模块级全局 ``wcwidth``
# （``screens.py`` 里 ``wcwidth = lru_cache(...)(_wcwidth)``，``draw`` 与
# ``display`` 都是调用时按名字查）。改这个全局即可让它与排版共用一张表，
# 不必改 vendor 里的第三方代码。
#
# 注入保守宽度而不是基础宽度：模型比物理终端宽，最坏是行尾空出一格；模型比
# 终端窄，物理终端就会自己折行，后续的按行绝对定位整体错位。
pyte.screens.wcwidth = conservative_char_width

# 触发备用缓冲区切换的序列（进入 / 退出）。
_SWITCH_ON = (b"\x1b[?1049h", b"\x1b[?1047h", b"\x1b[?47h")
_SWITCH_OFF = (b"\x1b[?1049l", b"\x1b[?1047l", b"\x1b[?47l")
_ALL_SWITCHES = _SWITCH_ON + _SWITCH_OFF
_MAX_SEQ = max(len(s) for s in _ALL_SWITCHES)


class _Screen(pyte.Screen):
    """``pyte.Screen`` 加自建回滚区。

    不用 ``pyte.HistoryScreen``：它在 ``__getattribute__`` 里拦截**每一次**
    属性访问，而 ``draw()`` 每写一个字符都要访问 ``buffer`` / ``cursor`` /
    ``columns``。整屏重绘会把开销推到 ``feed()`` 的一半以上，让每次整屏重绘
    从约 10ms 涨到 23ms —— 全屏程序连续重绘时，这些毫秒累积成肉眼可见的卡顿。

    这里只在**真正发生滚动**时（``index()`` 把顶行挤出去）记一行文本，
    其余路径与普通 ``Screen`` 完全一致。
    """

    def __init__(self, columns: int, lines: int, history: int = 2000) -> None:
        self._history: deque[str] = deque(maxlen=history or 1)
        self._keep_history = history > 0
        super().__init__(columns, lines)

    def index(self) -> None:
        # 顶行即将被挤出屏幕时先把它记进回滚区
        if self._keep_history and self.cursor.y == self.lines - 1:
            top = self.margins.top if self.margins else 0
            if top == 0:
                self._history.append(self._row_to_text(0))
        super().index()

    def _row_to_text(self, row: int) -> str:
        line = self.buffer[row]
        if not line:
            return ""
        cells = (line[x].data if x in line else " " for x in range(self.columns))
        return "".join(cells).rstrip()

    def history_lines(self) -> list[str]:
        return list(self._history)


def _tolerant(name: str):
    """给 CSI 处理函数加一层容错。

    pyte 对 ``CSI ? … <final>``（私有序列）一律以 ``private=True`` 调用处理
    函数，但 ``Stream.csi`` 里二十多个处理函数根本没有这个参数 —— 于是
    ``ESC [ ? 1 m``、``ESC [ ? 6 n``、``ESC [ ? 1 ; 2 c`` 这类终端真会发出的
    序列会让 ``feed()`` 直接抛 ``TypeError``，整个会话就此中断。

    这里退一步：先按原样调用；失败就去掉 ``private`` 再试；仍失败说明这个
    私有变体 pyte 不认识，按未知序列处理 —— 终端模拟器遇到读不懂的序列应该
    忽略，而不是把引擎带崩。
    """

    original = getattr(pyte.Screen, name)

    def handler(self, *args, **kwargs):
        try:
            return original(self, *args, **kwargs)
        except TypeError:
            pass
        kwargs.pop("private", None)
        try:
            return original(self, *args, **kwargs)
        except TypeError:
            return None

    handler.__name__ = name
    handler.__doc__ = "见 :func:`_tolerant`。"
    return handler


# 只为 pyte 真正会派发、且它自己没写对签名的那些处理函数包一层。
for _attr in sorted(set(pyte.streams.Stream.csi.values())):
    if getattr(_Screen, _attr, None) is getattr(pyte.Screen, _attr, None):
        setattr(_Screen, _attr, _tolerant(_attr))
del _attr



def _to_cell(char: Any) -> Cell:
    """把 pyte 的 Char 转成我们的 Cell（宽字符右半格归一到空 data）。"""
    if char is None:
        return BLANK
    data = char.data
    if data is None:
        data = ""
    # 多码点序列（emoji ZWJ 等）只取首码点，保证一格一个字符。
    if len(data) > 1:
        data = data[0]
    return Cell(
        data=data,
        fg=char.fg,
        bg=char.bg,
        bold=char.bold,
        italics=char.italics,
        underscore=char.underscore,
        reverse=char.reverse,
        strikethrough=char.strikethrough,
    )


class PyteScreenModel:
    """基于 pyte 的 :class:`ScreenModel` 实现（主/备双屏 + 按序切换）。"""

    def __init__(self, rows: int, columns: int, history: int = 2000) -> None:
        self._rows = rows
        self._cols = columns
        self._main = self._make(rows, columns, history)
        self._alt = self._make(rows, columns, 0)
        self._cur = self._main
        # 每张屏独立一个 ByteStream（避开 pyte 把 listener 绑死的问题）
        self._streams: dict[int, pyte.ByteStream] = {
            id(self._main): pyte.ByteStream(self._main),
            id(self._alt): pyte.ByteStream(self._alt),
        }
        self._stream = self._streams[id(self._main)]
        self._pending = b""  # 可能被块边界切断的开关序列前缀

    def _make(self, rows: int, columns: int, history: int) -> _Screen:
        # 注意参数顺序：Screen(columns, lines)
        return _Screen(columns, rows, history=history)

    # ---- 缓冲区切换 ----

    def _switch(self, entering: bool) -> None:
        already = (self._cur is self._alt) == entering
        if already:
            return
        target = self._alt if entering else self._main
        if entering:
            # 1049 语义：进入备用缓冲区即清屏
            target.reset()
        self._cur = target
        self._stream = self._streams[id(target)]
        self._mark_all_dirty()

    def _mark_all_dirty(self) -> None:
        self._cur.dirty.update(range(self._rows))

    @staticmethod
    def _find_switch(buf: bytes) -> tuple[int, int, bool] | None:
        """在 buf 中找最早的开关序列，返回 (位置, 长度, 是否进入)。"""
        best: tuple[int, int, bool] | None = None
        for seq, entering in ((s, True) for s in _SWITCH_ON):
            pos = buf.find(seq)
            if pos >= 0 and (best is None or pos < best[0]):
                best = (pos, len(seq), entering)
        for seq, entering in ((s, False) for s in _SWITCH_OFF):
            pos = buf.find(seq)
            if pos >= 0 and (best is None or pos < best[0]):
                best = (pos, len(seq), entering)
        return best

    def _partial_prefix_len(self, buf: bytes) -> int:
        """buf 末尾是否为某个开关序列的**前缀**（被块边界切断），返回需保留的长度。

        只有当最后一个 ESC 起的尾巴是开关序列的前缀时才保留，
        避免把 ``\\x1b[31m`` 这类正常序列误判成半截。
        """
        i = buf.rfind(b"\x1b")
        if i < 0:
            return 0
        tail = buf[i:]
        if len(tail) > _MAX_SEQ:
            return 0
        return len(tail) if any(p.startswith(tail) for p in _ALL_SWITCHES) else 0

    # ---- ScreenModel ----

    @property
    def rows(self) -> int:
        return self._rows

    @property
    def columns(self) -> int:
        return self._cols

    @property
    def cursor(self) -> Cursor:
        c = self._cur.cursor
        return Cursor(x=c.x, y=c.y, hidden=getattr(c, "hidden", False))

    @property
    def alternate(self) -> bool:
        return self._cur is self._alt

    @property
    def dirty(self) -> frozenset[int]:
        return frozenset(self._cur.dirty)

    def clear_dirty(self) -> None:
        self._cur.dirty.clear()

    def reset(self) -> None:
        """回到干净状态：两张屏清空、光标归位、解析器状态丢弃。

        覆盖层每换一屏内容都从零开始喂（它的模型就是那一屏），所以需要这条
        比 ``resize`` 更彻底的路径。
        """
        for screen in (self._main, self._alt):
            screen.reset()
        self._cur = self._main
        self._stream = self._streams[id(self._main)]
        self._pending = b""

    def feed(self, data: bytes) -> None:
        """喂入字节流；在开关序列处切换缓冲区（支持开关与内容同块）。"""
        buf = self._pending + data
        self._pending = b""
        while buf:
            found = self._find_switch(buf)
            if found is None:
                keep = self._partial_prefix_len(buf)
                if keep:
                    self._stream.feed(buf[:-keep])
                    self._pending = buf[-keep:]
                else:
                    self._stream.feed(buf)
                return
            pos, length, entering = found
            if pos:
                self._stream.feed(buf[:pos])
            self._switch(entering)
            buf = buf[pos + length :]

    def resize(self, rows: int, columns: int) -> None:
        self._rows = rows
        self._cols = columns
        for screen in (self._main, self._alt):
            # pyte 的 resize(lines, columns)，与构造顺序相反。
            screen.resize(rows, columns)
        self._mark_all_dirty()

    # ---- 行读取 ----

    def _cells(self, row: int) -> list[Cell]:
        line = self._cur.buffer.get(row, {})
        return [_to_cell(line.get(x)) for x in range(self._cols)]

    def row_cells(self, row: int) -> tuple[Cell, ...]:
        return tuple(self._cells(row))

    def row_text(self, row: int) -> str:
        if not 0 <= row < self._rows:
            return ""
        return "".join(c.data for c in self._cells(row)).rstrip()

    def lines(self) -> list[str]:
        return [self.row_text(r) for r in range(self._rows)]

    def scrollback(self, limit: int | None = None) -> list[str]:
        rows: list[str] = self._cur.history_lines()
        return rows[-limit:] if limit else rows

    def text(self, *, limit: int | None = None, include_scrollback: bool = True) -> str:
        lines: list[str] = list(self.scrollback()) if include_scrollback else []
        lines.extend(self.lines())
        while lines and not lines[-1].strip():
            lines.pop()
        if limit is not None and limit > 0:
            lines = lines[-limit:]
        return "\n".join(lines)
