"""引擎的 stdin 读路径：字节不丢，ESC 按到达时刻判。

两条都真出过问题：

* 旧写法每次 ``ensure_future(queue.get())`` 再 ``cancel()``：get 恰好在被取消
  前完成时，那块字节就再也不出现（长行、粘贴、连按方向键偶发少一段）；
* ESC 只从「处理到这里」起等 50ms，方向键的 ``[A`` 晚一步到就被判成独立
  Esc，随后的 ``O/A/C`` 漏进子进程。
"""

from __future__ import annotations

import asyncio
import time
from collections import deque

import pytest

from lls.session.engine import Engine
from lls.session.errors import Interrupted, SessionEnded


def _engine() -> Engine:
    """只备好循环内要用的原语，不碰 backend / screen。"""
    engine = Engine(
        backend=None,  # type: ignore[arg-type]
        argv=["sh"],
        screen=None,  # type: ignore[arg-type]
        handlers={},
        initial_handler="default",
    )
    engine._stdin_buf = deque()
    engine._stdin_ready = asyncio.Event()
    engine._exit_event = asyncio.Event()
    return engine


def test_a_wait_that_times_out_loses_nothing() -> None:
    """空等一场之后到达的字节照样取得到（旧写法会把它取消掉）。"""

    async def run():
        engine = _engine()
        first = await engine._wait_stdin(0.01)
        assert first is not None and first[1] == b""
        engine._push_stdin(b"def")
        got = await engine._wait_stdin(0.01)
        return got

    got = asyncio.run(run())
    assert got is not None and got[1] == b"def"


def test_bytes_arriving_just_as_the_wait_expires_are_kept() -> None:
    """等待还没被叫醒时字节就到了：仍要拿到它，不能被超时取消。"""

    async def run():
        engine = _engine()
        waiter = asyncio.ensure_future(engine._wait_stdin(0.05))
        engine._push_stdin(b"\x1b[A")
        return await waiter

    got = asyncio.run(run())
    assert got is not None and got[1] == b"\x1b[A"


def test_an_arrow_key_arriving_late_is_still_one_key() -> None:
    """ESC 之后 20ms 才到的 "[A"：按到达时刻算在窗口内，是上方向键。"""

    async def run():
        engine = _engine()
        now = time.monotonic()
        engine._push_stdin(b"\x1b", ts=now)
        engine._push_stdin(b"[A", ts=now + 0.02)
        key = await engine.read_key()
        engine._exit_event.set()
        return key

    key = asyncio.run(run())
    assert key is not None and key.control == "up"


def test_a_lone_escape_ends_up_as_the_escape_key() -> None:
    """真的只按了 Esc：等到 ESCDELAY 到点，判为 Esc 键。"""

    async def run():
        engine = _engine()
        engine._push_stdin(b"\x1b")
        key = await engine.read_key()
        engine._exit_event.set()
        return key

    key = asyncio.run(run())
    assert key is not None and key.control == "escape"


class _Pty:
    """记下写进子进程的字节。"""

    def __init__(self) -> None:
        self.written = []

    def write(self, data: bytes) -> None:
        self.written.append(data)


def test_wait_keys_forwards_terminal_responses_and_holds_typed_keys() -> None:
    """等网络时：终端应答当场转交，用户敲的字留到下一次读输入。"""

    async def run():
        engine = _engine()
        engine._pty = _Pty()
        engine._push_stdin("ab\x1b[12;40R".encode())
        await engine.wait_keys(0.05)
        return engine

    engine = asyncio.run(run())
    assert engine._pty.written == [b"\x1b[12;40R"]
    assert [key.raw for key in engine._held_keys] == [b"a", b"b"]


def test_wait_keys_stops_on_ctrl_c_but_keeps_what_came_first() -> None:
    """中断只放弃这一段等待，同批先敲下的字仍然属于用户。"""

    async def run():
        engine = _engine()
        engine._push_stdin(b"x\x03")
        with pytest.raises(Interrupted):
            await engine.wait_keys(0.05)
        return engine

    engine = asyncio.run(run())
    assert [key.raw for key in engine._held_keys] == [b"x"]


def test_held_keys_come_back_in_order_at_the_next_read() -> None:
    """攒下的键在下次读输入时按原顺序出现，且比后来的字节更早。"""

    async def run():
        engine = _engine()
        engine._push_stdin(b"ls")
        await engine.wait_keys(0.05)
        engine._push_stdin(b"-l")
        return [await asyncio.wait_for(engine.read_key(), 1) for _ in range(4)]

    assert [key.raw for key in asyncio.run(run())] == [b"l", b"s", b"-", b"l"]


def test_wait_keys_ends_when_the_child_is_gone() -> None:
    async def run():
        engine = _engine()
        engine._exit_event.set()
        with pytest.raises(SessionEnded):
            await engine.wait_keys(0.05)

    asyncio.run(run())
