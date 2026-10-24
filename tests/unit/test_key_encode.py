"""具名按键编码：名字 → 终端字节。

编码器与 ``KeyParser``（解码）对称：模型说 ``ctrl-c``、``up``，由这里翻成
真正的终端序列。名字大小写不敏感，失败时必须报出可用的名字。
"""

from __future__ import annotations

import pytest

from lls.tty import KEY_NAMES, KeyNameError, encode_key, encode_keys


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("enter", b"\r"),
        ("return", b"\r"),
        ("tab", b"\t"),
        ("backtab", b"\x1b[Z"),
        ("shift-tab", b"\x1b[Z"),
        ("backspace", b"\x7f"),
        ("bs", b"\x7f"),
        ("esc", b"\x1b"),
        ("escape", b"\x1b"),
        ("space", b" "),
        ("up", b"\x1b[A"),
        ("down", b"\x1b[B"),
        ("right", b"\x1b[C"),
        ("left", b"\x1b[D"),
        ("home", b"\x1b[H"),
        ("end", b"\x1b[F"),
        ("insert", b"\x1b[2~"),
        ("delete", b"\x1b[3~"),
        ("pageup", b"\x1b[5~"),
        ("pagedown", b"\x1b[6~"),
        ("f1", b"\x1bOP"),
        ("f2", b"\x1bOQ"),
        ("f3", b"\x1bOR"),
        ("f4", b"\x1bOS"),
        ("f5", b"\x1b[15~"),
        ("f6", b"\x1b[17~"),
        ("f7", b"\x1b[18~"),
        ("f8", b"\x1b[19~"),
        ("f9", b"\x1b[20~"),
        ("f10", b"\x1b[21~"),
        ("f11", b"\x1b[23~"),
        ("f12", b"\x1b[24~"),
        ("ctrl-a", b"\x01"),
        ("ctrl-c", b"\x03"),
        ("ctrl-d", b"\x04"),
        ("ctrl-z", b"\x1a"),
        ("ctrl-space", b"\x00"),
        ("ctrl-@", b"\x00"),
        ("ctrl-[", b"\x1b"),
        ("ctrl-\\", b"\x1c"),
        ("ctrl-]", b"\x1d"),
        ("ctrl-^", b"\x1e"),
        ("ctrl-_", b"\x1f"),
        ("alt-x", b"\x1bx"),
        ("alt-enter", b"\x1b\r"),
    ],
)
def test_named_keys_encode_to_their_sequences(name: str, expected: bytes) -> None:
    assert encode_key(name) == expected


def test_names_are_case_insensitive_and_trimmed() -> None:
    assert encode_key("CTRL-C") == b"\x03"
    assert encode_key(" Up ") == b"\x1b[A"


def test_ctrl_and_meta_short_aliases() -> None:
    assert encode_key("c-a") == b"\x01"
    assert encode_key("m-x") == b"\x1bx"
    assert encode_key("M-Enter") == b"\x1b\r"


def test_alt_supports_any_single_character() -> None:
    assert encode_key("alt-中") == b"\x1b" + "中".encode("utf-8")


def test_multiple_keys_are_concatenated_in_order() -> None:
    assert encode_keys(["up", "up", "enter"]) == b"\x1b[A\x1b[A\r"


def test_empty_key_list_encodes_to_nothing() -> None:
    assert encode_keys([]) == b""


@pytest.mark.parametrize("name", ["", "   ", "nope", "alt-xy"])
def test_unknown_names_raise(name: str) -> None:
    with pytest.raises(KeyNameError):
        encode_key(name)


def test_unknown_name_mentions_examples() -> None:
    with pytest.raises(KeyNameError) as error:
        encode_key("nope")

    message = str(error.value)
    assert "enter" in message
    assert "ctrl-c" in message


def test_key_names_table_exposes_the_common_keys() -> None:
    assert KEY_NAMES["enter"] == b"\r"
    assert KEY_NAMES["up"] == b"\x1b[A"
    assert KEY_NAMES["ctrl-c"] == b"\x03"
