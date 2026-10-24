"""显示原语单元测试：换行、覆盖层定位、按模型渲染。"""

from __future__ import annotations

from lls.term import PyteScreenModel
from lls.ui import (
    LinePrinter,
    char_width,
    content_start,
    print_screen_perfect,
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


# ---- print_screen_perfect ----

def test_print_screen_perfect_border() -> None:
    c = FakeConsole()
    screen = PyteScreenModel(rows=3, columns=10)
    screen.feed(b"hi")
    print_screen_perfect(screen, c, end="\r\n")
    out = c.snapshot()
    assert b"+--------+" in out  # width = columns-2 = 8
    assert b"hi" in out


def test_print_screen_perfect_cursor_highlight() -> None:
    c = FakeConsole()
    screen = PyteScreenModel(rows=3, columns=10)
    screen.feed(b"hi")  # 光标在 x=2
    print_screen_perfect(screen, c, end="\r\n")
    out = c.snapshot()
    assert b"\x1b[7m" in out  # 光标反色高亮
    assert b"\x1b[0m" in out


def test_print_screen_perfect_status_line() -> None:
    c = FakeConsole()
    screen = PyteScreenModel(rows=3, columns=10)
    screen.feed(b"x")
    print_screen_perfect(screen, c, end="\r\n")
    out = c.snapshot()
    assert b"cursor:" in out
    assert b"buffer:" in out


# ---- content_start ----
#
# 模型顶部可能留有空行（启动时对物理光标位置的对齐），这些行不是要显示的
# 内容。整屏显示必须从第一个非空行开始，否则真实内容会被搬上屏幕顶部。


def test_content_start_skips_leading_blank_rows() -> None:
    screen = PyteScreenModel(rows=6, columns=10)
    screen.feed(b"\n\n\nabc")  # 前三行为空，模拟光标对齐留下的空洞
    assert content_start(screen) == 3


def test_content_start_empty_screen_uses_cursor_row() -> None:
    screen = PyteScreenModel(rows=6, columns=10)
    screen.feed(b"\n\n")
    # 全空时不应返回负数或越界，跟随光标
    assert content_start(screen) == screen.cursor.y
