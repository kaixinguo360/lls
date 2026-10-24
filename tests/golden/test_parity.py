"""pyte 适配器与屏幕模型的对拍 / 不变量测试。

不依赖真实 pty，全部在内存中驱动屏幕模型。
"""

from __future__ import annotations

import random

import pytest

from lls.term import Cell, PyteScreenModel
from lls.term.snapshot import capture, diff


def _model(rows: int = 6, columns: int = 10, history: int = 2000) -> PyteScreenModel:
    return PyteScreenModel(rows=rows, columns=columns, history=history)


# ---- 基础行为 ----

def test_basic_text() -> None:
    m = _model()
    m.feed(b"abc")
    assert m.lines()[0] == "abc"
    assert m.cursor.x == 3
    assert m.cursor.y == 0


def test_linefeed_and_wrap() -> None:
    m = _model(rows=4, columns=6)
    m.feed(b"abcdef\r\nXY")
    assert m.lines()[:2] == ["abcdef", "XY"]
    assert m.cursor.x == 2
    assert m.cursor.y == 1


def test_incremental_utf8_across_chunks() -> None:
    """跨块截断的多字节字符必须正确解码——这是 R-UTF8 的回归测试。"""
    m = _model()
    b = "中文".encode()
    m.feed(b[:1])
    m.feed(b[1:2])
    m.feed(b[2:])
    assert m.lines()[0] == "中文"
    assert m.cursor.x == 4  # 2 + 2


def test_wide_char_cursor_advance() -> None:
    m = _model()
    m.feed("中文".encode())
    assert m.cursor.x == 4
    cells = m.row_cells(0)
    # 第一格是「中」，第二格被占用（空 data 且无样式）
    assert cells[0].data == "中"
    assert cells[1].data == ""
    assert cells[1].is_blank


def test_color_attribute() -> None:
    m = _model()
    m.feed(b"\x1b[31mX\x1b[0m")
    assert m.row_cells(0)[0].fg == "red"


def test_cursor_hidden() -> None:
    m = _model()
    m.feed(b"\x1b[?25l")
    assert m.cursor.hidden is True
    m.feed(b"\x1b[?25h")
    assert m.cursor.hidden is False


def test_alternate_buffer_switch() -> None:
    """?1049 必须真正切换缓冲区——pyte 本身不做，适配器补齐。"""
    m = _model(rows=3, columns=8)
    m.feed(b"main\r\n")
    assert m.alternate is False
    m.feed(b"\x1b[?1049h")
    assert m.alternate is True
    # 进入备用缓冲即清屏
    assert all(l == "" for l in m.lines())
    m.feed(b"ALT")
    assert m.lines()[0] == "ALT"
    m.feed(b"\x1b[?1049l")
    assert m.alternate is False
    # 回到主缓冲，主屏内容应仍在
    assert m.lines()[0] == "main"


def test_alternate_buffer_cursor_and_color() -> None:
    """备用缓冲区下光标定位/颜色必须生效。

    回归：pyte 的 Stream 在构造时把 listener 绑死，事后改 `stream.listener`
    只对纯文本生效，CSI/ESC 仍发给旧屏 —— 曾导致 alt 下文字进 alt、光标进 main，
    模型半坏（vim 场景表现为乱码）。
    """
    m = _model(rows=6, columns=20)
    m.feed(b"\x1b[?1049h")
    m.feed(b"\x1b[1;1H\x1b[31mRED\x1b[0m")
    m.feed(b"\x1b[2;1H\x1b[32mGREEN\x1b[0m")
    m.feed(b"\x1b[3;5H")

    assert m.lines()[0] == "RED"
    assert m.lines()[1] == "GREEN"
    assert m.row_cells(0)[0].fg == "red"
    assert m.row_cells(1)[0].fg == "green"
    assert (m.cursor.x, m.cursor.y) == (4, 2)

    # 退出后主屏不受污染
    m.feed(b"\x1b[?1049l")
    assert m.alternate is False
    assert all(l == "" for l in m.lines())


def test_dirty_tracking_and_clear() -> None:
    m = _model(rows=3, columns=8)
    m.feed(b"a")
    assert 0 in m.dirty
    m.clear_dirty()
    assert m.dirty == frozenset()
    m.feed(b"b")
    assert 0 in m.dirty


def test_scrollback() -> None:
    m = _model(rows=3, columns=8, history=10)
    for i in range(6):
        m.feed(f"line{i}\r\n".encode())
    # 6 行进 3 行屏：每个 \r\n 末尾的换行会把最旧行滚入历史，
    # 最终可见 line4/line5，line0..line3 进入回滚区。
    assert m.lines() == ["line4", "line5", ""]
    sb = m.scrollback()
    assert "line0" in sb[0]
    assert "line3" in sb[-1]


def test_text_trims_trailing_blank_lines() -> None:
    m = _model(rows=3, columns=8)
    m.feed(b"one\r\ntwo\r\n")
    text = m.text()
    # 末尾空行被去掉
    assert text == "one\ntwo"


def test_resize_arg_order() -> None:
    """pyte resize(lines, columns) 与构造顺序相反，适配器必须隐藏这一点。"""
    m = _model(rows=4, columns=8)
    m.resize(rows=10, columns=20)
    assert m.rows == 10
    assert m.columns == 20


# ---- 分块不变性（不变量 1）----

@pytest.mark.parametrize("chunk", [1, 2, 3, 7, 16])
def test_chunk_invariance(chunk: int) -> None:
    data = (
        b"\x1b[2J\x1b[Hhello world\r\n"
        + b"\x1b[31m".join([b"", b"colored"]) + b"\x1b[0m\r\n"
        + "中文".encode()
        + b"\r\n\x1b[3;5Hmiddle"
    )
    ref = _model()
    ref.feed(data)
    base = capture(ref, "ref")

    m = _model()
    for i in range(0, len(data), chunk):
        m.feed(data[i : i + chunk])
    d = diff(base, capture(m, f"chunk={chunk}"))
    assert d == [], d


def test_random_chunk_invariance() -> None:
    rng = random.Random(42)
    data = (
        b"\x1b[2J\x1b[Hone\r\ntwo\r\n\x1b[31mthree\x1b[0m\r\n"
        + "宽字符".encode()
        + b"\r\n\x1b[5;3Hcursor"
    )
    ref = _model(rows=8, columns=20)
    ref.feed(data)
    base = capture(ref, "ref")

    for _ in range(20):
        m = _model(rows=8, columns=20)
        pos = 0
        while pos < len(data):
            n = rng.randint(1, 9)
            m.feed(data[pos : pos + n])
            pos += n
        d = diff(base, capture(m, "random"))
        assert d == [], d
