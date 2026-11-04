"""``scroll`` 命令：把终端历史（回滚区 + 当前屏）交给 pager。

命令本身只负责取内容、解析行数上限；翻页与按键全在 pager 里。
"""

from __future__ import annotations

import asyncio

from lls.commands.builtin import agent_pager, core


class FakeScreen:
    rows = 5
    columns = 20

    def __init__(self, text: str) -> None:
        self._text = text
        self.calls: list[tuple] = []

    def text(self, *, limit=None, include_scrollback: bool = True) -> str:
        self.calls.append((limit, include_scrollback))
        return self._text


class FakeServices:
    def __init__(self, text: str) -> None:
        self.screen = FakeScreen(text)
        self.messages: list[str] = []

    async def show_message(self, text: str) -> None:
        self.messages.append(text)


def _run(monkeypatch, services, args):
    shown: dict = {}

    async def fake_page(s, lines, footer="") -> None:
        shown["lines"] = list(lines)
        shown["footer"] = footer

    monkeypatch.setattr(agent_pager, "page", fake_page)
    asyncio.run(core.cmd_scroll(services, args))
    return shown


def test_scroll_hands_the_history_to_the_pager(monkeypatch) -> None:
    services = FakeServices("一\n二\n三")

    shown = _run(monkeypatch, services, None)

    assert shown["lines"] == ["一", "二", "三"]
    assert "q" in shown["footer"]
    assert services.screen.calls[-1][0] == core.DEFAULT_SCROLL_LINES


def test_scroll_takes_a_line_limit(monkeypatch) -> None:
    services = FakeServices("x")

    _run(monkeypatch, services, "50")

    assert services.screen.calls[-1][0] == 50


def test_scroll_rejects_a_bad_limit(monkeypatch) -> None:
    services = FakeServices("x")

    shown = _run(monkeypatch, services, "abc")

    assert shown == {}
    assert "用法" in services.messages[-1]


def test_scroll_says_so_when_there_is_nothing(monkeypatch) -> None:
    services = FakeServices("")

    shown = _run(monkeypatch, services, None)

    assert shown == {}
    assert "还没有内容" in services.messages[-1]
