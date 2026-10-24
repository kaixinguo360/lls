"""按键解析器单元测试。"""

from __future__ import annotations

from lls.tty import KeyParser


def test_visible_chars() -> None:
    kp = KeyParser()
    keys = kp.feed(b"ab")
    assert [k.char for k in keys] == ["a", "b"]


def test_enter_and_backspace() -> None:
    kp = KeyParser()
    keys = kp.feed(b"a\r")
    assert keys[0].char == "a"
    assert keys[1].control == "enter"
    keys = kp.feed(b"\x7f")
    assert keys[0].control == "backspace"


def test_ctrl_keys() -> None:
    kp = KeyParser()
    keys = kp.feed(b"\x05\x07\x03")
    assert [k.control for k in keys] == ["ctrl_e", "ctrl_g", "ctrl_c"]


def test_arrow_keys() -> None:
    kp = KeyParser()
    keys = kp.feed(b"\x1b[A")
    assert keys[0].control == "up"


def test_utf8_split_across_feed() -> None:
    kp = KeyParser()
    b = "中".encode()
    assert kp.feed(b[:1]) == []
    assert kp.feed(b[1:])[0].char == "中"


def test_no_key_dropped() -> None:
    """一次 feed 多个键必须全部产出，不丢键。"""
    kp = KeyParser()
    keys = kp.feed(b"abc\r")
    assert len(keys) == 4
    assert keys[3].control == "enter"


# ---- ESC 歧义：独立 Esc 键 vs 序列前缀 ----
#
# ESC 既可以是独立按键，也可以是方向键等序列的前缀（ESC [ A）。
# 解析器先挂起，调用方超时后用 flush_escape 判定为独立 Esc。

def test_lone_escape_is_pending_then_flushed() -> None:
    kp = KeyParser()
    # 单独 ESC：不能立刻产出按键（可能后面还跟着序列）
    assert kp.feed(b"\x1b") == []
    assert kp.pending_escape == b"\x1b"
    # 超时后判定为独立 Esc
    key = kp.flush_escape()
    assert key is not None
    assert key.control == "escape"
    assert kp.pending_escape == b""


def test_escape_sequence_not_flushed_as_escape() -> None:
    """完整的方向键序列不应被误判成 Esc 键。"""
    kp = KeyParser()
    assert kp.feed(b"\x1b") == []
    keys = kp.feed(b"[A")
    assert [k.control for k in keys] == ["up"]
    assert kp.pending_escape == b""
    # 序列已消费，flush 不应再产出任何东西
    assert kp.flush_escape() is None


def test_escape_then_regular_key() -> None:
    """ESC 后面紧跟普通字符：ESC 作为独立键，字符照常解析。

    回归：早期实现把两者拼成 ``ESC x`` 判为 unknown 并整体丢弃，
    导致用户按 Esc 退出后立刻敲的字符一起消失。
    """
    kp = KeyParser()
    assert kp.feed(b"\x1b") == []
    keys = kp.feed(b"x")
    assert len(keys) == 2
    assert keys[0].control == "escape"
    assert keys[1].char == "x"
    assert kp.pending_escape == b""
