"""``wait`` 与 ``sleep``：都睡满调用方点名的那一段，区别只在要不要看。

跟 ``read_terminal`` 的 ``wait_seconds`` 不是一回事：那个等「静默」，画面一
安静就提前回来；这两个等的是**时间**，中间有没有输出都一样。它们也不能被
中途的按键打断 —— ``port.wait`` 会因为键盘上的事提前返回，工具得对着截止
时刻接着等。
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from lls.ai.tools import ScreenView, terminal_tools
from lls.ai.tools.terminal import MAX_SLEEP


class _Screen:
    rows = 24
    columns = 80
    alternate = False

    def __init__(self, text: str = "prompt $") -> None:
        self._text = text

    def text(self, limit: int = 0, include_scrollback: bool = True) -> str:
        return self._text

    @property
    def cursor(self) -> SimpleNamespace:
        return SimpleNamespace(x=0, y=0, hidden=False)


class _Port:
    """``instant`` 时每轮只让出 0 秒就返回，像真实引擎处理了一个按键。"""

    def __init__(self, text: str = "prompt $", *, instant: bool = False) -> None:
        self.screen = _Screen(text)
        self.instant = instant
        self.waits: list[float] = []

    def write(self, data: bytes, *, enter: bool = False) -> None:
        pass

    def flush_model(self) -> None:
        pass

    async def wait(self, timeout: float) -> None:
        self.waits.append(timeout)
        if self.instant:
            await asyncio.sleep(0)
        else:
            await asyncio.sleep(timeout)

    def resize(self, rows: int, columns: int) -> tuple[int, int]:
        return rows, columns


def _tools(port: _Port) -> dict:
    view = ScreenView(port, port.wait)
    return {entry.tool.name: entry for entry in terminal_tools(port, view)}


def test_wait_sleeps_then_returns_the_screen() -> None:
    port = _Port("server up $")

    started = time.monotonic()
    result = asyncio.run(_tools(port)["wait"].run({"seconds": 0.05, "lines": 5}))
    elapsed = time.monotonic() - started

    assert elapsed >= 0.045
    assert "<status>waited</status>" in result
    assert "server up $" in result


def test_sleep_waits_but_returns_no_screen() -> None:
    port = _Port("server up $")

    started = time.monotonic()
    result = asyncio.run(_tools(port)["sleep"].run({"seconds": 0.05}))
    elapsed = time.monotonic() - started

    assert elapsed >= 0.045
    assert "server up $" not in result
    assert "<terminal_state" not in result
    assert "已等待" in result
    assert isinstance(result.data["seconds"], float)


def test_sleep_offers_no_lines_parameter() -> None:
    parameters = _tools(_Port())["sleep"].tool.parameters

    assert list(parameters["properties"]) == ["seconds"]
    assert parameters["required"] == ["seconds"]


def test_early_returns_from_the_loop_do_not_cut_the_sleep_short() -> None:
    """按键让 ``wait`` 提前返回时，工具必须接着等，不能当成睡够了。"""
    port = _Port(instant=True)

    started = time.monotonic()
    asyncio.run(_tools(port)["wait"].run({"seconds": 0.05, "lines": 5}))
    elapsed = time.monotonic() - started

    assert elapsed >= 0.045
    assert len(port.waits) > 1


def test_seconds_are_clamped_to_the_maximum() -> None:
    class _Bail(Exception):
        pass

    class _BailingPort(_Port):
        async def wait(self, timeout: float) -> None:
            self.waits.append(timeout)
            raise _Bail

    port = _BailingPort()
    view = ScreenView(port, port.wait)

    with pytest.raises(_Bail):
        asyncio.run(view.sleep(10 * MAX_SLEEP))

    assert 0 < port.waits[0] <= MAX_SLEEP


def test_negative_seconds_is_rejected() -> None:
    tools = _tools(_Port())

    for name in ("wait", "sleep"):
        with pytest.raises(ValueError):
            asyncio.run(tools[name].run({"seconds": -1, "lines": 5}))


def test_both_tools_are_registered_after_the_other_waits() -> None:
    names = list(_tools(_Port()))

    assert names.index("wait_for_change") < names.index("wait")
    assert names.index("wait") < names.index("sleep")
