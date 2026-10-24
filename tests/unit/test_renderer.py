"""显示原语单元测试：换行、覆盖层定位、按模型渲染。"""

from __future__ import annotations

import pyte

from lls.session.engine import Engine
from lls.term import PyteScreenModel
from lls.ui import (
    LinePrinter,
    char_width,
    wrap_multi_lines,
)


class FakeConsole:
    def __init__(self) -> None:
        self.data = bytearray()

    def write(self, data: bytes) -> None:
        self.data.extend(data)

    def snapshot(self) -> bytes:
        out = bytes(self.data)
        self.data.clear()
        return out


# ---- char_width / wrap_multi_lines ----

def test_char_width() -> None:
    assert char_width("a") == 1
    assert char_width("中") == 2


def test_wrap_multi_lines() -> None:
    out, lines = wrap_multi_lines("123456789", 8)
    assert lines == 2
    assert "\r\n" in out


# ---- LinePrinter ----

def test_print_lines_writes_content() -> None:
    c = FakeConsole()
    p = LinePrinter(c)
    p.print_lines("hello", None, 80)
    out = c.snapshot()
    assert b"\x1b[2K\r" in out  # 清当前行 + 回车
    assert b"hello" in out


def test_print_lines_cursor_positioning() -> None:
    """光标不在末尾时：回车 + 写前缀定位（不用反色高亮）。

    单行（lines_all=1）时不上移，直接 ``\\r`` + 写前缀 ``text[:cursor]``。
    """
    c = FakeConsole()
    p = LinePrinter(c)
    p.print_lines("> abcd", cursor=4, width=80)
    out = c.snapshot()
    # 写完整内容后回车，再写前缀 "> ab"，光标停在 x=4
    assert b"> abcd" in out
    assert out.endswith(b"\r> ab")
    assert b"\x1b[7m" not in out  # 不用反色高亮


def test_clear_lines() -> None:
    c = FakeConsole()
    p = LinePrinter(c)
    p.clear_lines(2, 1)
    out = c.snapshot()
    assert b"\x1b[2K" in out
    assert b"\x1b[1A" in out


def test_print_lines_clears_each_wrapped_row() -> None:
    """退格让一行变短时，旧字符不能留在后面几行上。"""
    c = FakeConsole()
    p = LinePrinter(c)
    p.print_lines("abcdefgh", None, 4, anchor_row=0)
    first = c.snapshot()

    p.print_lines("abcdef", None, 4, anchor_row=0)
    second = c.snapshot()

    assert second.count(b"\x1b[2K") == 2  # 两行都先清后写
    screen = pyte.Screen(4, 2)
    stream = pyte.ByteStream(screen)
    stream.feed(first)
    stream.feed(second)
    assert [line.rstrip() for line in screen.display] == ["abcd", "ef"]


# ---- 覆盖层变矮：多出来的旧行必须按模型恢复 ----

def _overlay_engine(rows: int = 8, columns: int = 20):
    screen = PyteScreenModel(rows=rows, columns=columns)
    console = FakeConsole()
    engine = Engine(
        backend=None,  # type: ignore[arg-type]
        argv=[],
        screen=screen,
        handlers={},
        initial_handler="default",
    )
    engine._console = console
    engine._printer = LinePrinter(console)
    engine._loop = None
    return engine, screen, console


def _emulate(data: bytes, rows: int = 8, columns: int = 20):
    screen = pyte.Screen(columns, rows)
    pyte.ByteStream(screen).feed(data)
    return [line.rstrip() for line in screen.display]


def test_shrinking_overlay_restores_the_rows_it_used_to_cover() -> None:
    engine, screen, console = _overlay_engine()
    screen.feed(b"one\r\ntwo\r\nthree")

    engine.draw_overlay("a\nb\nc")  # 3 行，占模型光标起的 3 行
    console.snapshot()              # 丢掉第一帧
    engine.draw_overlay("x")        # 1 行：旧覆盖层的其余行要还原成模型内容

    rows = _emulate(bytes(console.data))
    assert rows[2].rstrip() == "x"
    assert rows[3].rstrip() == ""
    assert rows[4].rstrip() == ""


def test_backspace_clears_the_old_tail_of_a_wrapped_prompt() -> None:
    """退格后旧字符不能留在折行后的末行上（用户直接看到的症状）。"""
    engine, _screen, console = _overlay_engine(rows=6, columns=8)

    engine.draw_overlay("> abcdefgh", cursor=10)
    first = console.snapshot()
    engine.draw_overlay("> abcdefg", cursor=9)
    second = console.snapshot()

    rows = _emulate(first + second, rows=6, columns=8)
    assert rows[0].rstrip() == "> abcdef"
    assert rows[1].rstrip() == "g"

