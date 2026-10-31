"""read_line 的补全轮盘与行尾提示：回放按键，看屏幕。"""

from __future__ import annotations

import asyncio
from collections import deque

import pyte

from lls.commands.catalog import CommandMeta, completer_for
from lls.session.engine import Engine
from lls.term import PyteScreenModel

SLASH = (
    CommandMeta("help", ("h", "?"), "/help"),
    CommandMeta("status", ("s",), "/status"),
    CommandMeta("title", (), "/title [文字]"),
)
COMPLETE = completer_for(SLASH, marker="/", path_escape=True)


class FakeConsole:
    def __init__(self) -> None:
        self.data = bytearray()
        self.writes = 0

    def write(self, data: bytes) -> None:
        self.data.extend(data)
        self.writes += 1

    def snapshot(self) -> bytes:
        out = bytes(self.data)
        self.data.clear()
        return out


def _run(
    keys: bytes,
    completer=None,
    *,
    rows=8,
    columns=40,
    completion_style="redis",
    **kwargs
):
    async def go():
        screen = PyteScreenModel(rows=rows, columns=columns)
        console = FakeConsole()
        engine = Engine(
            backend=None,  # type: ignore[arg-type]
            argv=[],
            screen=screen,
            handlers={},
            initial_handler="default",
        )
        engine._attach_console(console)
        engine._stdin_buf = deque()
        engine._stdin_ready = asyncio.Event()
        engine._exit_event = asyncio.Event()
        screen.feed(b"$ ")
        engine.push_keys(keys)
        line = await engine.read_line(
            "> ", completer=completer, completion_style=completion_style, **kwargs
        )
        return line, bytes(console.data), engine

    return asyncio.run(go())


def _screen(data: bytes, rows: int = 8, columns: int = 40):
    screen = pyte.Screen(columns, rows)
    pyte.ByteStream(screen).feed(data)
    return screen


def test_tab_previews_the_first_candidate_and_enter_accepts_it() -> None:
    line, _, _ = _run(b"/st\t\r", COMPLETE)
    assert line == "/status "


def test_tab_cycles_through_the_candidates_in_place() -> None:
    line, _, _ = _run(b"/\t\t\r", COMPLETE)
    assert line == "/status "


def test_a_whole_turn_comes_back_to_the_original_text_and_bells() -> None:
    line, out, _ = _run(b"/" + b"\t" * 4 + b"\r", COMPLETE)
    assert line == "/"
    assert b"\a" in out


def test_escape_only_drops_the_preview() -> None:
    line, _, _ = _run(b"/st\t\x1bq\r", COMPLETE, escape="esc")
    assert line == "/stq"


def test_other_keys_accept_the_preview_before_being_applied() -> None:
    line, _, _ = _run(b"/st\tx\r", COMPLETE)
    assert line == "/status x"


def test_tab_in_the_middle_of_the_line_only_bells() -> None:
    line, out, _ = _run(b"/st\x1b[D\t\r", COMPLETE)
    assert line == "/st"
    assert b"\a" in out


def test_without_a_completer_tab_keeps_its_old_behaviour() -> None:
    line, out, _ = _run(b"ab\tc\r")
    assert line == "abc"
    assert b"\a" not in out


def test_the_hint_is_painted_gray_after_the_line() -> None:
    line, out, _ = _run(b"/title\r", COMPLETE, handoff=lambda _line: True)
    assert line == "/title"
    screen = _screen(out)
    text = "\n".join(screen.display)
    assert "[文字]" in text
    for y, row in enumerate(screen.display):
        x = row.find("文字")
        if x >= 0:
            assert screen.buffer[y][x].fg == "brightblack"
            break
    else:  # pragma: no cover - 找不到就是没画
        raise AssertionError("hint not painted")


def test_a_long_hint_is_cut_instead_of_wrapping_the_overlay() -> None:
    line, out, engine = _run(
        b"/title\r", COMPLETE, handoff=lambda _line: True, columns=12
    )
    assert line == "/title"
    assert engine.overlay.lines == 1
    assert "文字" not in "\n".join(_screen(out, columns=12).display)

# ---- ghost 风格：行尾虚影 ----

def test_ghost_shows_the_remaining_text() -> None:
    line, out, _ = _run(
        b"/st\r", COMPLETE, completion_style="ghost", handoff=lambda _line: True
    )
    assert line == "/st"
    assert "atus" in "\n".join(_screen(out).display)


def test_ghost_shows_the_usage_tail() -> None:
    line, out, _ = _run(
        b"/ti\r", COMPLETE, completion_style="ghost", handoff=lambda _line: True
    )
    assert line == "/ti"
    text = "\n".join(_screen(out).display)
    assert "tle" in text
    assert "[文字]" in text


def test_ghost_marks_more_candidates_with_an_ellipsis() -> None:
    line, out, _ = _run(
        b"/\r", COMPLETE, completion_style="ghost", handoff=lambda _line: True
    )
    assert line == "/"
    text = "\n".join(_screen(out).display)
    assert "help" in text and "…" in text


def test_ghost_tab_accepts_the_suggestion() -> None:
    line, _, _ = _run(b"/st\t\r", COMPLETE, completion_style="ghost")
    assert line == "/status "


def test_ghost_right_arrow_accepts_the_suggestion() -> None:
    line, _, _ = _run(b"/st\x1b[C\r", COMPLETE, completion_style="ghost")
    assert line == "/status "


def test_ghost_right_arrow_still_moves_when_there_is_nothing_to_accept() -> None:
    line, _, _ = _run(b"xy\x1b[D\x1b[C\r", COMPLETE, completion_style="ghost")
    assert line == "xy"


def test_ghost_is_painted_gray() -> None:
    line, out, _ = _run(
        b"/st\r", COMPLETE, completion_style="ghost", handoff=lambda _line: True
    )
    assert line == "/st"
    screen = _screen(out)
    for y, row in enumerate(screen.display):
        x = row.find("atus")
        if x >= 0:
            assert screen.buffer[y][x].fg == "brightblack"
            break
    else:  # pragma: no cover - 找不到就是没画
        raise AssertionError("ghost not painted")