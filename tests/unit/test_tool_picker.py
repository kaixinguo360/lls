"""开关界面：拨动、跟随、保存、取消。

只测「按键 → 状态」这条线；覆盖层怎么画由 Services 负责，这里用假的。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from lls.commands.builtin.tool_picker import ToolSwitch, toggle_tools


def _key(control: str = "", char: str = "", raw: bytes = b"") -> SimpleNamespace:
    return SimpleNamespace(control=control, char=char, raw=raw)


class FakeServices:
    def __init__(self, keys) -> None:
        self._keys = list(keys)
        self.screen = SimpleNamespace(rows=10, columns=60)
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


def _items() -> list[ToolSwitch]:
    return [
        ToolSwitch(name="read_terminal", enabled=True, source="global", fallback=True),
        ToolSwitch(name="write_file", enabled=True, source="global", fallback=True),
    ]


def test_space_flips_the_cursor_row_and_marks_it_session_owned() -> None:
    services = FakeServices([_key("down"), _key(char=" "), _key("enter")])

    result = asyncio.run(toggle_tools(services, _items()))

    assert result is not None
    assert result[0] == ToolSwitch("read_terminal", True, "global", True)
    assert result[1] == ToolSwitch("write_file", False, "session", True)
    assert any("[off] write_file" in frame for frame in services.frames)


def test_r_goes_back_to_following_the_global_value() -> None:
    services = FakeServices([_key(char=" "), _key(char="r"), _key("enter")])

    result = asyncio.run(toggle_tools(services, _items()))

    assert result is not None
    assert result[0] == ToolSwitch("read_terminal", True, "global", True)


def test_following_is_disabled_for_the_global_layer() -> None:
    services = FakeServices([_key(char="r"), _key(char=" "), _key("enter")])

    result = asyncio.run(toggle_tools(services, _items(), allow_follow=False))

    assert result is not None
    assert result[0].source == "session"
    assert result[0].enabled is False


def test_the_source_column_is_shown() -> None:
    services = FakeServices([_key("enter")])

    asyncio.run(toggle_tools(services, _items()))

    assert any("global" in frame for frame in services.frames)


def test_enter_returns_the_same_states() -> None:
    services = FakeServices([_key("enter")])

    assert asyncio.run(toggle_tools(services, _items())) == _items()


def test_q_cancels_and_returns_none() -> None:
    services = FakeServices([_key(char=" "), _key(char="q")])

    assert asyncio.run(toggle_tools(services, _items())) is None


def test_escape_cancels_too() -> None:
    services = FakeServices([_key("escape")])

    assert asyncio.run(toggle_tools(services, _items())) is None


def test_the_overlay_source_is_handed_back_on_exit() -> None:
    services = FakeServices([_key("enter")])

    asyncio.run(toggle_tools(services, _items()))

    assert services.source is None
