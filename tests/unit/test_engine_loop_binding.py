"""引擎不得在构造函数里创建 asyncio 原语。

``app.run_interactive`` 在 ``asyncio.run()`` **之前**构造 ``Engine``。Python 3.8
上 ``asyncio.Event`` / ``asyncio.Queue`` 构造时即绑定当前事件循环，而
``asyncio.run`` 随后另起一个循环，两者一旦相遇就无法 await。

因此这些原语必须等循环跑起来再创建 —— 本测试守住这一点。
"""

from __future__ import annotations

import asyncio

from lls.session.engine import Engine

PRIMITIVES = (asyncio.Queue, asyncio.Event, asyncio.Lock, asyncio.Semaphore)


def _engine() -> Engine:
    # 构造只做赋值，不碰 backend / screen 的方法。
    return Engine(
        backend=None,  # type: ignore[arg-type]
        argv=["sh"],
        screen=None,  # type: ignore[arg-type]
        handlers={},
        initial_handler="default",
    )


def test_construction_creates_no_asyncio_primitives() -> None:
    engine = _engine()

    bound = [name for name, value in vars(engine).items() if isinstance(value, PRIMITIVES)]

    assert bound == [], f"构造函数里创建了 asyncio 原语：{bound}"


def test_construction_does_not_touch_the_event_loop() -> None:
    """连 ``get_event_loop()`` 都不该碰：那会提前创建出默认循环。"""
    calls = []
    original = asyncio.get_event_loop

    def spy():
        calls.append(1)
        return original()

    asyncio.get_event_loop = spy  # type: ignore[assignment]
    try:
        _engine()
    finally:
        asyncio.get_event_loop = original  # type: ignore[assignment]

    assert calls == []
