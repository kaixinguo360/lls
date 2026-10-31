"""开关界面：拨动、保存、取消。

只测「按键 → 状态」这条线；覆盖层怎么画由 Services 负责，这里用假的。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from lls.commands.builtin.tool_picker import toggle_tools


def _key(control: str = "", char: str = "", raw: bytes = b"") -> SimpleNamespace:
    return SimpleNamespace(control=control, char=char, raw=raw)


class FakeServices:
    def __init__(self, keys) -> None:
        self._keys = list(keys)
        self.screen = SimpleNamespace(rows=10, columns=40)
        self.frames: list[str] = []
        self.source = None

    async def read_key(self):
        if not self._keys:
            await asyncio.sleep(0.5)
            return None
        return self._keys.pop(0)

    def set_overlay_source(self, source) -> None:
        self.source = source

    def draw_overlay(self, frame: str) -> None:
        self.frames.append(frame)


def test_space_flips_the_cursor_row_and_enter_returns_the_states() -> None:
    services = FakeServices([_key("down"), _key(char=" "), _key("enter")])

    result = asyncio.run(toggle_tools(services, ["a", "b"], {"a": True, "b": True}))

    assert result == {"a": True, "b": False}
    assert any("[off] b" in frame for frame in services.frames)


def test_enter_without_touching_anything_returns_the_same_states() -> None:
    services = FakeServices([_key("enter")])

    result = asyncio.run(toggle_tools(services, ["a"], {"a": True}))

    assert result == {"a": True}


def test_q_cancels_and_returns_none() -> None:
    services = FakeServices([_key(char=" "), _key(char="q")])

    result = asyncio.run(toggle_tools(services, ["a"], {"a": True}))

    assert result is None


def test_escape_cancels_too() -> None:
    services = FakeServices([_key("escape")])

    assert asyncio.run(toggle_tools(services, ["a"], {"a": True})) is None


def test_the_overlay_source_is_handed_back_on_exit() -> None:
    services = FakeServices([_key("enter")])

    asyncio.run(toggle_tools(services, ["a"], {"a": True}))

    assert services.source is None
