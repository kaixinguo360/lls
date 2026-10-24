"""引擎不得在构造函数里创建 asyncio 原语。

``app.run_interactive`` 是在 ``asyncio.run()`` **之前**构造 ``Engine`` 的。在
Python 3.8 里，``asyncio.Event`` / ``asyncio.Queue`` 会在构造时就绑定当前事件
循环（``events.get_event_loop()``），3.10 才改成惰性绑定。于是 3.8 上这些原语
绑在默认循环上，``asyncio.run`` 又新建一个循环，一 await 就报：

    RuntimeError: ... got Future <Future pending> attached to a different loop

两者都必须等循环跑起来再创建。这个测试守的就是「别把它们放回 __init__」。
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

    assert bound == [], "构造时创建了 asyncio 原语（3.8 会绑定到错误的循环）：%s" % bound


def test_construction_does_not_touch_the_event_loop() -> None:
    """连 get_event_loop() 都不该碰：它会让「默认循环」被提前创建出来。"""
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
