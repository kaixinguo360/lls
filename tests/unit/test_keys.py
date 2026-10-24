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
