"""整屏 pager：把一段行文本按屏翻滚，末行固定一行状态。

只做排版与按键，不认识会话、模型这些概念 —— 调用方给什么就显示什么。
绘制复用 ``Services.draw_overlay``：文本恰好 ``screen.rows`` 行时正好铺满
一屏。按键来自 ``Services.read_key``：方向键这类序列会被终端拆成几次
read，必须由引擎的按键解析器拼装，自己按字节猜会误判。
"""

from __future__ import annotations

import asyncio
from typing import Any, List, Sequence

from lls.ui import conservative_char_width, text_width, wrap_multi_lines

__all__ = ["Pager", "page"]

_REVERSE = "\x1b[7m"
_RESET = "\x1b[0m"

# 没有按键时也隔一会儿看一次窗口尺寸：拖动终端边框要能立刻重排。
POLL_SECONDS = 0.2

# 按键 → 滚动意图。名字取自引擎的按键解析（``Key.control`` / ``Key.char``）。
_BY_CONTROL = {
    "up": "up",
    "down": "down",
    "pageup": "page_up",
    "pagedown": "page_down",
    "home": "first",
    "end": "last",
}

_BY_CHAR = {
    "j": "down",
    "k": "up",
    "f": "page_down",
    "b": "page_up",
    "d": "half_down",
    "u": "half_up",
    "g": "first",
    "G": "last",
    " ": "page_down",
}

# PgUp/PgDn 不在解析器的已知按键表里（保持原样转发给子进程），
# 因此这里按原始序列再认一次；只在 pager 内部生效。
_BY_RAW = {
    b"\x1b[5~": "page_up",
    b"\x1b[6~": "page_down",
    b"\x1b[1~": "first",
    b"\x1b[4~": "last",
}


def _wrap(line: str, columns: int) -> List[str]:
    """按列宽折行；行内的 SGR 由 ``wrap_multi_lines`` 跨行重放。"""
    if columns <= 0:
        return [line]
    output, _ = wrap_multi_lines(
        line, columns, end="\n", measure=conservative_char_width
    )
    return output.split("\n")


def _clip(line: str, columns: int) -> str:
    """按列宽截断，超出用省略号收尾（末行因此永远只占一行）。"""
    if columns <= 0:
        return ""
    if text_width(line, conservative_char_width) <= columns:
        return line
    room = columns - conservative_char_width("…")
    kept = ""
    used = 0
    for char in line:
        width = conservative_char_width(char)
        if used + width > room:
            break
        kept += char
        used += width
    return kept + "…"


def _intent(key: Any) -> str:
    """把一个按键翻成滚动意图；``quit`` 表示收起，空串表示无动作。

    只读 ``control`` / ``char`` / ``raw`` 三个属性，测试可以给任意替身。
    """
    control = getattr(key, "control", "") or ""
    char = getattr(key, "char", "") or ""
    raw = getattr(key, "raw", b"") or b""
    if control in ("escape", "ctrl_c") or char in ("q", "Q"):
        return "quit"
    if raw in _BY_RAW:
        return _BY_RAW[raw]
    if control in _BY_CONTROL:
        return _BY_CONTROL[control]
    if control == "enter":
        return "down"
    return _BY_CHAR.get(char, "")


# 初值：大于任何可能的底部，等同于"开局贴底"。
_BELOW_BOTTOM = 1 << 30


class Pager:
    """一屏的滚动状态：正文若干行 + 末行一行状态。

    开局停在底部：要看的总是最新的一句，历史往上翻。之后若用户没往上翻，
    窗口变尺寸时仍贴着底部（正读着最新一行时光标不会跳走）。
    """

    def __init__(
        self, lines: Sequence[str], footer: str = "", *, rows: int, columns: int
    ) -> None:
        self._source = list(lines)
        self._footer = footer or ""
        self._rows = rows
        self._columns = columns
        self._lines: List[str] = [""]
        self._height = 0
        # 首次排版前把 top 顶到"超出任何底部"，让 _layout 判定为贴底。
        self._top = _BELOW_BOTTOM
        self._layout()

    def _layout(self) -> None:
        """按当前尺寸重排正文与末行；``top`` 夹回可见范围。

        原来就贴在底部（``top`` 到达旧底部）则重排后继续保持贴底。
        """
        follow = self._top >= self._bottom()
        self._columns = max(1, self._columns)
        self._height = max(0, self._rows - 1)
        wrapped: List[str] = []
        for line in self._source:
            wrapped.extend(_wrap(line, self._columns))
        self._lines = wrapped or [""]
        self._footer_line = _clip(" ".join(self._footer.split()), self._columns)
        self._top = self._bottom() if follow else min(self._top, self._bottom())

    @property
    def top(self) -> int:
        """当前屏首行在全文中的下标。"""
        return self._top

    @property
    def lines(self) -> List[str]:
        return list(self._lines)

    def resize(self, rows: int, columns: int) -> bool:
        """窗口尺寸变了就重排一次；返回是否真的变了。"""
        if (rows, columns) == (self._rows, self._columns):
            return False
        self._rows, self._columns = rows, columns
        self._layout()
        return True

    def frame(self) -> str:
        """这一屏：正文 ``height`` 行 + 一行反显状态。"""
        body = self._lines[self._top : self._top + self._height]
        rows = list(body) + [""] * (self._height - len(body))
        return "\n".join(rows + [_REVERSE + self._footer_line + _RESET])

    def _bottom(self) -> int:
        return max(0, len(self._lines) - self._height)

    def scroll(self, delta: int) -> bool:
        new = min(self._bottom(), max(0, self._top + delta))
        if new == self._top:
            return False
        self._top = new
        return True

    def first(self) -> bool:
        return self.scroll(-len(self._lines) - 1)

    def last(self) -> bool:
        return self.scroll(len(self._lines) + 1)

    def handle(self, key: Any) -> str:
        """处理一个按键，返回 ``quit`` / ``redraw`` / ``none``。"""
        intent = _intent(key)
        if intent == "quit":
            return "quit"
        return "redraw" if self._apply(intent) else "none"

    def _apply(self, intent: str) -> bool:
        step = max(1, self._height)
        if intent == "down":
            return self.scroll(1)
        if intent == "up":
            return self.scroll(-1)
        if intent == "page_down":
            return self.scroll(step)
        if intent == "page_up":
            return self.scroll(-step)
        if intent == "half_down":
            return self.scroll(max(1, step // 2))
        if intent == "half_up":
            return self.scroll(-max(1, step // 2))
        if intent == "first":
            return self.first()
        if intent == "last":
            return self.last()
        return False


def _size(services: Any) -> tuple:
    screen = services.screen
    return (
        int(getattr(screen, "rows", 24) or 24),
        int(getattr(screen, "columns", 80) or 80),
    )


async def page(services: Any, lines: Sequence[str], footer: str = "") -> None:
    """铺满一屏显示 ``lines``，按 q/Esc/Ctrl-C 收起。

    重排有两条路：窗口变化由引擎经 ``set_overlay_source`` 立刻回调 ``render``；
    另有 ``POLL_SECONDS`` 的兜底轮询，宿主不送 SIGWINCH 时也能跟上尺寸。
    """
    rows, columns = _size(services)
    pager = Pager(lines, footer, rows=rows, columns=columns)

    def render(rows: int, columns: int) -> str:
        """按新尺寸重排后再取一屏：引擎在窗口变化时直接调它。"""
        pager.resize(rows, columns)
        return pager.frame()

    services.set_overlay_source(render)
    services.draw_overlay(pager.frame())
    reader = asyncio.ensure_future(services.read_key())
    try:
        while True:
            done, _ = await asyncio.wait({reader}, timeout=POLL_SECONDS)
            resized = pager.resize(*_size(services))
            moved = False
            if done:
                key = reader.result()
                if key is None:
                    break
                action = pager.handle(key)
                if action == "quit":
                    break
                moved = action == "redraw"
                reader = asyncio.ensure_future(services.read_key())
            if resized or moved:
                services.draw_overlay(pager.frame())
    finally:
        reader.cancel()
        services.set_overlay_source(None)
        services.restore_context()
