"""宿主状态序列扫描器：只认三类，任意切分都得认得出来。"""

from __future__ import annotations

import pytest

from lls.term.host_sequences import MAX_PENDING, HostSequences

TITLE = b"\x1b]2;\xe6\xad\xa3\xe5\x9c\xa8\xe7\xbc\x96\xe8\xaf\x91\x07"  # OSC 2;正在编译
STYLE = b"\x1b[5 q"
CLIP = b"\x1b]52;c;5L2g5aW9\x07"


def _scan(*parts: bytes) -> HostSequences:
    seq = HostSequences()
    for part in parts:
        seq.feed(part)
    return seq


def test_reads_all_three() -> None:
    seq = _scan(TITLE + STYLE + CLIP)

    assert seq.title == "正在编译"
    assert seq.cursor_style == 5
    assert seq.clipboard == "5L2g5aW9"


@pytest.mark.parametrize("cut", range(1, len(TITLE)))
def test_title_survives_any_split(cut: int) -> None:
    assert _scan(TITLE[:cut], TITLE[cut:]).title == "正在编译"


def test_cursor_style_survives_any_split() -> None:
    for cut in range(1, len(STYLE)):
        assert _scan(STYLE[:cut], STYLE[cut:]).cursor_style == 5


def test_cursor_style_without_a_parameter() -> None:
    assert _scan(b"\x1b[ q").cursor_style == 0
    assert _scan(b"\x1b[", b" q").cursor_style == 0
    assert _scan(b"\x1b", b"[2 q").cursor_style == 2


def test_an_osc_can_end_with_st() -> None:
    assert _scan(b"\x1b]52;c;YWJj\x1b\\").clipboard == "YWJj"


def test_other_sequences_are_left_alone() -> None:
    seq = _scan(b"\x1b[1;31mred\x1b[0m\x1b]0;title\x07")

    assert seq.cursor_style is None
    assert seq.title == "title"


def test_a_title_keeps_no_control_characters() -> None:
    assert _scan(b"\x1b]2;ab\x01cd\x07").title == "abcd"


def test_an_unterminated_osc_does_not_grow_forever() -> None:
    seq = HostSequences()
    for _ in range(20):
        seq.feed(b"\x1b]52;c;" + b"A" * 1024)

    assert len(seq._pending) <= MAX_PENDING
    assert seq.clipboard is None


def test_a_lone_escape_waits_for_the_next_chunk() -> None:
    assert _scan(b"\x1b", b"]0;x\x07").title == "x"


def test_reset_forgets_everything() -> None:
    seq = _scan(TITLE + STYLE + CLIP)

    seq.reset()

    assert (seq.title, seq.cursor_style, seq.clipboard) == ("", None, None)
