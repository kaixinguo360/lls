"""agent_picker: a picker that does not fill the screen, plus its keys."""

from __future__ import annotations

import asyncio

from lls.commands.builtin.agent_picker import (
    DEFAULT_FOOTER,
    ELLIPSIS,
    Picker,
    pick,
)
from lls.tty import Key


def _picker(items=None, labels=None, rows=10, columns=40, cursor=0, header=""):
    items = items if items is not None else ["a", "b", "c"]
    labels = labels if labels is not None else list(items)
    return Picker(
        items, labels, rows=rows, columns=columns, cursor=cursor, header=header
    )


def test_the_module_is_plain_ascii() -> None:
    import lls.commands.builtin.agent_picker as module

    with open(module.__file__, "rb") as handle:
        assert handle.read().isascii()


def _key(control="", char="", raw=b""):
    return Key(char=char, control=control, raw=raw)


def test_frame_is_only_as_tall_as_the_candidates() -> None:
    screen = _picker(["a", "b", "c"]).frame().split("\n")
    # three candidates plus one footer line, not all 10 rows
    assert len(screen) == 4
    assert screen[0].startswith("\x1b[7m")
    assert screen[0].endswith("a\x1b[0m")
    assert screen[1] == "  b"


def test_frame_grows_a_window_when_candidates_outgrow_the_screen() -> None:
    picker = _picker(["line {}".format(i) for i in range(30)], rows=6)
    screen = picker.frame().split("\n")
    assert len(screen) == 5            # rows - 2 candidates plus the footer
    assert screen[0].endswith("line 0\x1b[0m")


def test_header_sits_above_and_costs_one_row() -> None:
    picker = _picker(["line {}".format(i) for i in range(30)], rows=6, header="sessions")
    screen = picker.frame().split("\n")
    assert screen[0] == "sessions"
    assert len(screen) == 5            # one row less for candidates, header on top
    assert screen[-1].endswith("1/30")


def test_no_header_means_no_extra_line() -> None:
    screen = _picker(["line 0", "line 1"]).frame().split("\n")
    assert len(screen) == 3            # two candidates and the footer
    assert screen[-1] == "1/2"


def test_cursor_moves_and_the_window_follows() -> None:
    picker = _picker(["line {}".format(i) for i in range(30)], rows=6)
    for _ in range(10):
        assert picker.handle(_key(control="down", raw=b"\x1b[B")) == "redraw"
    assert picker.cursor == 10
    screen = picker.frame().split("\n")
    assert screen[-2].endswith("line 10\x1b[0m")


def test_edges_do_not_redraw() -> None:
    picker = _picker()
    assert picker.handle(_key(control="up", raw=b"\x1b[A")) == "none"
    assert picker.handle(_key(char="G")) == "redraw"
    assert picker.handle(_key(control="down", raw=b"\x1b[B")) == "none"
    assert picker.handle(_key(control="home")) == "redraw"
    assert picker.handle(_key(control="home")) == "none"


def test_enter_accepts_and_escape_quits() -> None:
    picker = _picker()
    assert picker.handle(_key(char="j")) == "redraw"
    assert picker.handle(_key(control="enter")) == "accept"
    assert picker.selected() == "b"
    assert picker.handle(_key(control="escape")) == "quit"
    assert picker.handle(_key(char="q")) == "quit"
    assert picker.handle(_key(control="ctrl_c")) == "quit"


def test_cursor_starts_on_the_current_item_and_is_clamped() -> None:
    picker = _picker(cursor=2)
    assert picker.cursor == 2
    assert _picker(cursor=99).cursor == 2
    assert _picker(cursor=-5).cursor == 0


def test_long_labels_are_clipped() -> None:
    screen = _picker(["x" * 100], columns=20).frame().split("\n")
    assert len(screen[0]) <= 20 + len("\x1b[7m>\x1b[0m") + 1
    assert screen[0].endswith(ELLIPSIS + "\x1b[0m")
    assert all(ord(char) < 128 for char in screen[0])


class FakeScreen:
    rows = 8
    columns = 20


class FakeServices:
    def __init__(self, keys=()) -> None:
        self.screen = FakeScreen()
        self._keys = list(keys)
        self.frames = []
        self.source = None
        self.restored = 0

    def draw_overlay(self, text, cursor=None):
        self.frames.append(text)
        return 0, len(text.split("\n"))

    def set_overlay_source(self, source=None) -> None:
        self.source = source

    async def read_key(self):
        return self._keys.pop(0) if self._keys else None

    def restore_context(self) -> None:
        self.restored += 1


def test_pick_returns_the_chosen_item() -> None:
    services = FakeServices([_key(char="j"), _key(control="enter")])
    chosen = asyncio.run(pick(services, ["a", "b", "c"], ["a", "b", "c"]))

    assert chosen == "b"
    assert len(services.frames) == 2           # first frame, then the redraw
    assert all(len(frame.split("\n")) == 4 for frame in services.frames)
    footer = services.frames[-1].split("\n")[-1]
    assert DEFAULT_FOOTER.startswith(footer[:9])
    assert all(frame.isascii() for frame in services.frames)
    assert services.restored == 1


def test_pick_returns_none_when_cancelled_or_empty() -> None:
    services = FakeServices([_key(char="q")])
    assert asyncio.run(pick(services, ["a"], ["a"])) is None
    assert services.restored == 1
    assert asyncio.run(pick(FakeServices(), [], [])) is None


def test_pick_restores_when_the_child_goes_away() -> None:
    services = FakeServices([])
    assert asyncio.run(pick(services, ["a"], ["a"])) is None
    assert services.restored == 1
    assert services.source is None
