"""屏幕模型的行为与不变式。

全部在内存里驱动字节流，不依赖真实 pty。核心不变式是**分块无关**：
``feed()`` 接受任意切分的字节流，切法不得影响最终状态。
"""

from __future__ import annotations

import random
from typing import Tuple

import pytest

from lls.term import PyteScreenModel

# 一个可比较的屏幕状态：(各行文本, 光标, 是否备用屏)
State = Tuple[Tuple[str, ...], Tuple[int, int, bool], bool]


def _model(rows: int = 6, columns: int = 10, history: int = 2000) -> PyteScreenModel:
    return PyteScreenModel(rows=rows, columns=columns, history=history)


def _state(model: PyteScreenModel) -> State:
    cursor = (model.cursor.x, model.cursor.y, model.cursor.hidden)
    return tuple(model.lines()), cursor, model.alternate


# ---- 基础行为 ----


def test_basic_text() -> None:
    m = _model()
    m.feed(b"abc")
    assert m.lines()[0] == "abc"
    assert (m.cursor.x, m.cursor.y) == (3, 0)


def test_linefeed_and_wrap() -> None:
    m = _model(rows=4, columns=6)
    m.feed(b"abcdef\r\nXY")
    assert m.lines()[:2] == ["abcdef", "XY"]
    assert (m.cursor.x, m.cursor.y) == (2, 1)


def test_incremental_utf8_across_chunks() -> None:
    """跨块截断的多字节字符必须正确解码。"""
    m = _model()
    raw = "中文".encode()
    for index in range(len(raw)):
        m.feed(raw[index : index + 1])
    assert m.lines()[0] == "中文"
    assert m.cursor.x == 4


def test_wide_char_cursor_advance() -> None:
    m = _model()
    m.feed("中文".encode())
    assert m.cursor.x == 4
    cells = m.row_cells(0)
    assert cells[0].data == "中"
    # 宽字符占两格，右半格是空 data 的占位格
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
    """``?1049`` 必须真正切换缓冲区，而不只是记下模式。"""
    m = _model(rows=3, columns=8)
    m.feed(b"main\r\n")
    assert m.alternate is False

    m.feed(b"\x1b[?1049h")
    assert m.alternate is True
    assert all(line == "" for line in m.lines())

    m.feed(b"ALT")
    assert m.lines()[0] == "ALT"

    m.feed(b"\x1b[?1049l")
    assert m.alternate is False
    assert m.lines()[0] == "main"


def test_alternate_buffer_cursor_and_color() -> None:
    """备用缓冲区里光标定位与颜色照样生效，且退出后不污染主屏。"""
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

    m.feed(b"\x1b[?1049l")
    assert m.alternate is False
    assert all(line == "" for line in m.lines())


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
    for index in range(6):
        m.feed(f"line{index}\r\n".encode())

    # 6 行滚过 3 行屏：可见 line4/line5，line0..line3 进回滚区
    assert m.lines() == ["line4", "line5", ""]
    scrollback = m.scrollback()
    assert "line0" in scrollback[0]
    assert "line3" in scrollback[-1]


def test_text_trims_trailing_blank_lines() -> None:
    m = _model(rows=3, columns=8)
    m.feed(b"one\r\ntwo\r\n")
    assert m.text() == "one\ntwo"


def test_resize_takes_rows_before_columns() -> None:
    """``resize`` 的参数顺序与构造一致（行在前）。"""
    m = _model(rows=4, columns=8)
    m.resize(rows=10, columns=20)
    assert (m.rows, m.columns) == (10, 20)


# ---- 分块无关 ----


@pytest.mark.parametrize("chunk", [1, 2, 3, 7, 16])
def test_chunk_invariance(chunk: int) -> None:
    data = (
        b"\x1b[2J\x1b[Hhello world\r\n"
        + b"\x1b[31m".join([b"", b"colored"])
        + b"\x1b[0m\r\n"
        + "中文".encode()
        + b"\r\n\x1b[3;5Hmiddle"
    )
    reference = _model()
    reference.feed(data)
    expected = _state(reference)

    m = _model()
    for offset in range(0, len(data), chunk):
        m.feed(data[offset : offset + chunk])

    assert _state(m) == expected


def test_random_chunk_invariance() -> None:
    rng = random.Random(42)
    data = (
        b"\x1b[2J\x1b[Hone\r\ntwo\r\n\x1b[31mthree\x1b[0m\r\n"
        + "宽字符".encode()
        + b"\r\n\x1b[5;3Hcursor"
    )
    reference = _model(rows=8, columns=20)
    reference.feed(data)
    expected = _state(reference)

    for _ in range(20):
        m = _model(rows=8, columns=20)
        offset = 0
        while offset < len(data):
            step = rng.randint(1, 9)
            m.feed(data[offset : offset + step])
            offset += step
        assert _state(m) == expected
