"""把阻塞调用挪到线程里跑。

3.8 没有 ``asyncio.to_thread``，而读整份会话日志、按预算算 token 这类调用
动辄几十毫秒起步：留在事件循环里，Ctrl-C 与画面刷新就一起被堵住。
"""

from __future__ import annotations

import asyncio
import functools
from typing import Any, Callable, TypeVar

__all__ = ["offload"]

T = TypeVar("T")


async def offload(func: Callable[..., T], *args: Any) -> T:
    """在线程里跑 ``func(*args)``，等它跑完再回到事件循环。"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, functools.partial(func, *args))
