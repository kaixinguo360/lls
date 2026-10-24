"""组合根：装配引擎与处理器并运行。

这里是唯一知道「有哪些处理器、初始用哪个」的地方。引擎本身不认识这些名字，
因此新增一种输入方式只需在 :func:`build_handlers` 里注册，无需改动引擎。
"""

from __future__ import annotations

import asyncio
import os
import sys

from .handlers import CommandHandler, PassthroughHandler
from .session.engine import Engine
from .session.services import Handler
from .term import PyteScreenModel
from .tty import make_backend

__all__ = ["run_interactive"]

# 绑定到默认处理器的字节：Ctrl-E 进命令模式。应用层约定，引擎不认识其含义。
_KEY_COMMAND = 0x05

# 命令模式读行时按 Esc 的返回哨兵
_ESCAPE = object()


def build_handlers() -> dict[str, Handler]:
    """构造处理器注册表。应用层唯一需要知道名字的地方。"""

    def on_error(detail: str) -> None:
        # 保留完整堆栈，供 err 命令查看（覆盖层只显示一行摘要）
        from .commands.builtin.core import record_error

        record_error(detail)

    return {
        "default": PassthroughHandler({_KEY_COMMAND: "command"}),
        "command": CommandHandler(":", on_error=on_error, escape=_ESCAPE),
    }


def run_interactive(command: list[str]) -> int:
    try:
        size = os.get_terminal_size()
    except OSError:
        size = os.terminal_size((80, 24))
    screen = PyteScreenModel(rows=size.lines, columns=size.columns)

    backend = make_backend()
    engine = Engine(backend, command, screen, build_handlers(), "default")

    try:
        return asyncio.run(engine.run())
    except RuntimeError as e:
        print(f"lls: {e}", file=sys.stderr)
        return 1
