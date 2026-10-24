"""显示原语单元测试（精确还原 legacy display.py / terminal.py）。"""

from __future__ import annotations

from lls.term import PyteScreenModel
from lls.ui import (
    LinePrinter,
    char_width,
    content_start,
    full_redraw,
    print_screen_perfect,
    restore_line,
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


# ---- full_redraw / content_start ----
#
# 回归：用户进入 LLS 时光标已在最后一行，模型起始行与之对齐后顶部会留出空行。
# 若重绘无条件从屏幕顶行开始，真实内容会被搬到屏幕顶部（表现为光标跳到顶行）。

def test_content_start_skips_leading_blank_rows() -> None:
    screen = PyteScreenModel(rows=6, columns=10)
    screen.feed(b"\n\n\nabc")  # 前三行为空，模拟光标对齐留下的空洞
    assert content_start(screen) == 3


def test_content_start_empty_screen_uses_cursor_row() -> None:
    screen = PyteScreenModel(rows=6, columns=10)
    screen.feed(b"\n\n")
    # 全空时不应返回负数或越界，跟随光标
    assert content_start(screen) == screen.cursor.y


def test_full_redraw_starts_at_content_not_screen_top() -> None:
    c = FakeConsole()
    screen = PyteScreenModel(rows=6, columns=10)
    screen.feed(b"\n\n\nabc")
    full_redraw(screen, c)
    out = c.snapshot()
    # 定位到第 4 行（内容起始行，1-based）而不是第 1 行
    assert out.startswith(b"\x1b[4;1H")
    # 清除操作从该行向下，不清掉上方未建模的画面
    assert b"\x1b[0J" in out
    assert b"\x1b[2J" not in out
    assert b"abc" in out


def test_full_redraw_keeps_cursor_on_model_row() -> None:
    c = FakeConsole()
    screen = PyteScreenModel(rows=10, columns=10)
    screen.feed(b"\n\n\nabc")
    row = screen.cursor.y
    full_redraw(screen, c)
    out = c.snapshot()
    # 光标定位应在模型光标所在行（1-based），而非屏幕顶部
    assert f"\x1b[{row + 1};{screen.cursor.x + 1}H".encode() in out


# ---- restore_line ----（单行恢复的光标精度）

def test_restore_line_positions_cursor_explicitly() -> None:
    """行尾空白被去掉后，光标仍须落在模型位置。

    回归：曾只写行文本、依赖「写完光标在末尾」，导致 shell 提示符 "$ "
    被去空格成 "$" 后，光标比原位少一位。
    """
    c = FakeConsole()
    screen = PyteScreenModel(rows=5, columns=20)
    screen.feed(b"\x1b[3;1H$ ")      # 提示符带尾空格
    screen.feed(b"\x1b[3;3H")        # 光标在第 3 列
    restore_line(screen, c)
    out = c.snapshot()
    assert b"\x1b[2K\r" in out
    assert b"$" in out
    # 显式定位到第 3 行第 3 列
    assert b"\x1b[3;3H" in out


def test_restore_line_cursor_mid_text() -> None:
    c = FakeConsole()
    screen = PyteScreenModel(rows=5, columns=20)
    screen.feed(b"\x1b[3;1Habcdefghij")
    screen.feed(b"\x1b[3;7H")
    restore_line(screen, c)
    out = c.snapshot()
    assert b"abcdefghij" in out
    assert b"\x1b[3;7H" in out


def test_restore_line_cursor_beyond_text() -> None:
    """光标在文本之后：纯文本长度推算不出列坐标，必须显式定位。"""
    c = FakeConsole()
    screen = PyteScreenModel(rows=5, columns=40)
    screen.feed(b"\x1b[3;1Habc")
    screen.feed(b"\x1b[3;20H")
    restore_line(screen, c)
    out = c.snapshot()
    assert b"\x1b[3;20H" in out


def test_restore_line_wide_chars() -> None:
    """宽字符：字符数与显示列数不等，仍需按列定位（中文 4 字占 8 列）。"""
    c = FakeConsole()
    screen = PyteScreenModel(rows=5, columns=40)
    screen.feed("\x1b[3;1H中文测试".encode())
    screen.feed(b"\x1b[3;9H")
    restore_line(screen, c)
    out = c.snapshot()
    assert "中文测试".encode() in out
    assert b"\x1b[3;9H" in out

