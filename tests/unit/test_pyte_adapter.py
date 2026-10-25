"""pyte 适配器的容错：读不懂的私有 CSI 不能把引擎带崩。

pyte 对 ``ESC [ ? … <final>`` 一律以 ``private=True`` 派发，而 ``Stream.csi``
里二十多个处理函数并没有这个参数。终端真会发这类序列（``?m``、``?6n``、
``?1;2c``），任何一个都足以让 ``feed()`` 抛 ``TypeError``、中断整个会话。
"""

from __future__ import annotations

import pytest
from pyte.streams import Stream

from lls.term import PyteScreenModel

# 实测会崩的私有序列，按 final 逐个覆盖
PRIVATE_SEQUENCES = [
    "\x1b[?1m",
    "\x1b[?1;2m",
    "\x1b[?6n",
    "\x1b[?1;2c",
    "\x1b[?1H",
    "\x1b[?1r",
    "\x1b[?1'",
    "\x1b[?1g",
]


@pytest.mark.parametrize("sequence", PRIVATE_SEQUENCES)
def test_private_csi_does_not_raise(sequence: str) -> None:
    model = PyteScreenModel(rows=10, columns=40)

    model.feed(sequence.encode())


@pytest.mark.parametrize("final", sorted(Stream.csi))
def test_every_private_final_is_tolerated(final: str) -> None:
    """穷举：任何一个 CSI final 配上私有前缀都不该抛异常。"""
    for params in ("1", "1;2"):
        model = PyteScreenModel(rows=10, columns=40)

        model.feed(("\x1b[?" + params + final).encode())


def test_engine_keeps_working_after_an_unknown_private_sequence() -> None:
    model = PyteScreenModel(rows=10, columns=40)

    model.feed(b"\x1b[?1m")
    model.feed(b"hello")

    assert model.row_text(0) == "hello"


def test_mode_switches_still_take_effect() -> None:
    model = PyteScreenModel(rows=10, columns=40)

    model.feed(b"\x1b[?25l")

    assert model.cursor.hidden is True


def test_alternate_screen_switch_still_takes_effect() -> None:
    model = PyteScreenModel(rows=10, columns=40)

    model.feed(b"\x1b[?1049h")
    assert model.alternate is True

    model.feed(b"\x1b[?1049l")
    assert model.alternate is False


def test_ordinary_sequences_are_unaffected() -> None:
    model = PyteScreenModel(rows=10, columns=40)

    model.feed(b"\x1b[2J\x1b[Hhello\x1b[1;31mred")

    assert model.row_text(0) == "hellored"