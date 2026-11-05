"""``wait_for_change``：等的是「画面变了」，不是「画面安静了」。

两者在待命场景里结果完全不同：``read_terminal`` 在本来就安静的屏幕上立刻
返回，拿它守着终端就是空转；``wait_for_change`` 必须一直等到内容真的变了。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from lls.ai.tools import ScreenView, terminal_tools


class _Screen:
    """按调用次数依次返回不同文本；用完之后一直返回最后一份。"""

    rows = 24
    columns = 80
    alternate = False

    def __init__(self, texts) -> None:
        self._texts = list(texts)
        self._calls = 0

    def text(self, limit: int = 0) -> str:
        index = min(self._calls, len(self._texts) - 1)
        self._calls += 1
        return self._texts[index]

    @property
    def cursor(self) -> SimpleNamespace:
        return SimpleNamespace(x=0, y=0, hidden=False)


class _Port:
    def __init__(self, texts) -> None:
        self.screen = _Screen(texts)
        self.flushed = 0

    def write(self, data: bytes, *, enter: bool = False) -> None:
        pass

    def flush_model(self) -> None:
        self.flushed += 1

    async def wait(self, timeout: float) -> None:
        await asyncio.sleep(0)

    def resize(self, rows: int, columns: int) -> tuple[int, int]:
        return rows, columns


def _view(texts) -> ScreenView:
    port = _Port(texts)
    return ScreenView(port, port.wait)


def test_returns_changed_as_soon_as_text_differs() -> None:
    view = _view(["prompt $", "prompt $", "done $"])

    text, status = asyncio.run(view.wait_for_change(24, timeout=5))

    assert status == "changed"
    assert text == "done $"


def test_times_out_when_nothing_changes() -> None:
    view = _view(["still"])

    text, status = asyncio.run(view.wait_for_change(24, timeout=0.05))

    assert status == "timeout"
    assert text == "still"


def test_waits_for_the_first_difference_not_for_quiet() -> None:
    """文本从第一份变成第二份就算数，不需要它先安静下来。"""
    view = _view(["a", "b", "c"])

    text, status = asyncio.run(view.wait_for_change(24, timeout=5))

    assert (text, status) == ("b", "changed")


def test_tool_is_registered_with_the_same_port() -> None:
    port = _Port(["x"])
    names = [entry.tool.name for entry in terminal_tools(port, port.wait)]

    assert "wait_for_change" in names
