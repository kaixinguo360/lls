"""无头引擎：不接宿主终端，只保留 pty、屏幕模型与录制这条链。

``lls serve`` 靠这一条：没有 console 可接管，也不能去读 stdin。这里用一个
真 pty 跑一条短命令，确认引擎照常把子进程输出喂进屏幕模型，并且 ``stop()``
能让主循环干净退出。
"""

from __future__ import annotations

import asyncio
import sys

import pytest

from lls.session.engine import Engine
from lls.term import PyteScreenModel
from lls.tty import make_backend

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="需要真 pty（Windows 上没有 pty/termios）"
)


class _Idle:
    """什么也不做的处理器：主循环反复调用它，直到有人 stop()。"""

    passthrough = False

    async def step(self, services) -> None:  # noqa: ANN001
        await asyncio.sleep(0.02)


def _engine(argv: list[str]) -> Engine:
    screen = PyteScreenModel(rows=24, columns=80)
    return Engine(
        make_backend(),
        argv,
        screen,
        {"idle": _Idle()},
        "idle",
        attach_host=False,
    )


def test_headless_engine_feeds_the_screen() -> None:
    engine = _engine(["sh", "-c", "printf headless-ok"])

    async def main() -> None:
        run = asyncio.ensure_future(engine.run())
        try:
            for _ in range(50):
                await asyncio.sleep(0.02)
                engine.flush_backlog()
                if "headless-ok" in engine.screen.text(limit=10):
                    break
            assert "headless-ok" in engine.screen.text(limit=10)
        finally:
            engine.stop()
            await asyncio.wait_for(run, 5)

    asyncio.run(main())


def test_stop_ends_the_loop_without_waiting_for_the_child() -> None:
    engine = _engine(["sh", "-c", "sleep 30"])

    async def main() -> None:
        run = asyncio.ensure_future(engine.run())
        await asyncio.sleep(0.1)
        assert not run.done()
        engine.stop()
        await asyncio.wait_for(run, 5)

    asyncio.run(main())


def test_headless_never_opens_the_host_console() -> None:
    engine = _engine(["sh", "-c", "printf x"])
    backend = engine._backend

    def refuse() -> None:
        raise AssertionError("无头运行不该接管宿主终端")

    backend.open_console = refuse  # type: ignore[method-assign]

    async def main() -> None:
        run = asyncio.ensure_future(engine.run())
        await asyncio.sleep(0.2)
        engine.stop()
        await asyncio.wait_for(run, 5)

    asyncio.run(main())
