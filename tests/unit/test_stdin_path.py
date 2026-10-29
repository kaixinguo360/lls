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

from lls.session.engine import Engine


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
