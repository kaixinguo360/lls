"""应用层输入处理器 —— 框架之外的一层。

引擎只反复调用 ``step()``，因此「怎样响应输入」完全在这里决定。本模块给出
两个通用处理器：

* :class:`PassthroughHandler` —— 子进程独占终端，输入原样转发（可绑定
  某些字节切到别的处理器）；
* :class:`CommandHandler` —— 读一行，按注册表分发为命令。

两者都不包含具体业务：命令逻辑在 :mod:`lls.commands` 里，本层只做分发。
"""

from __future__ import annotations

import inspect
import traceback
from typing import Awaitable, Callable

from lls.commands import execute_command
from lls.commands.result import CommandResult
from lls.session.services import Services

__all__ = ["CommandHandler", "PassthroughHandler", "split_command"]


def split_command(line: str) -> tuple[str, str | None]:
    """把 ``"name args..."`` 拆成 ``(name, args)``；args 为空时为 None。"""
    line = line.strip()
    if " " in line:
        cmd, _, args = line.partition(" ")
        return cmd.strip(), args.strip() or None
    return line, None


class PassthroughHandler:
    """子进程独占终端：输入原样转发，只拦截绑定的字节。

    ``bindings`` 把字节值映射到处理器名，例如 ``{0x05: "command"}``。
    引擎只认处理器名，不认识这几个字节的含义 —— 那是应用层的约定。
    """

    passthrough = True

    def __init__(self, bindings: dict[int, str] | None = None) -> None:
        self._bindings = dict(bindings or {})

    async def step(self, s: Services) -> None:
        data = await s.read_chars()
        if data is None:
            # 子进程已退出
            return
        if not data:
            return
        for i, byte in enumerate(data):
            target = self._bindings.get(byte)
            if target is not None:
                # 触发键之前的字节仍属于子进程
                if i:
                    s.write(data[:i])
                # 触发键之后同一批到达的字节属于**下一个**处理器：
                # 用户可能在一次 read 里先按 Ctrl-E 又立刻敲命令。
                # 丢弃会表现为「切过去之后按键失灵」，所以回填。
                if i + 1 < len(data):
                    s.push_keys(data[i + 1 :])
                # 切换前让模型追上：透传模式下显示优先、模型在后追赶，
                # 此刻模型可能还缺最后几屏输出（含子进程的光标定位）。
                # 不排空就用模型去定位覆盖层和物理光标，位置就是旧的。
                s.flush_model()
                s.set_handler(target)
                return
        s.write(data)


class CommandHandler:
    """读一行命令并按注册表分发。

    命令自己完成全部行为，只有「切处理器」和「退出」通过 ``CommandResult``
    返回 —— 那两件事需要引擎配合。
    """

    passthrough = False

    def __init__(
        self,
        prompt: str = ":",
        *,
        on_result: Callable[[Services, CommandResult], Awaitable[None]] | None = None,
        on_error: Callable[[str], None] | None = None,
        cancel: str | None = "q",
        escape: object | None = None,
        restore_on_exit: bool = True,
    ) -> None:
        self._prompt = prompt
        self._cancel = cancel
        self._escape = escape
        self._on_result = on_result
        self._on_error = on_error
        self._restore_on_exit = restore_on_exit

    async def step(self, s: Services) -> None:
        line = await s.read_line(
            self._prompt,
            include_last=False,
            cancel=self._cancel,
            history_id="line_mode",
            escape=self._escape,
        )
        if line is None:
            # 子进程退出
            return
        if line is self._escape:
            # Esc：退出本处理器回到透传。
            #
            # 同批到达的后续按键（按了 Esc 又立刻敲字）还留在键盘缓冲里，
            # 而透传层读的是原始字节队列 —— 两个队列不互通，不回填就丢了。
            s.requeue_pending_keys()
            s.set_handler("default")
            if self._restore_on_exit:
                s.restore_context()
            return
        if line == "":
            return
        if line == self._cancel:
            # Ctrl-C：等同退出回默认处理器
            s.requeue_pending_keys()
            s.set_handler("default")
            if self._restore_on_exit:
                s.restore_context()
            return
        cmd, args = split_command(line)
        try:
            result = await execute_command(cmd, s, args)
        except Exception as e:  # noqa: BLE001
            # 完整堆栈交给应用层留档（供 err 命令查看），覆盖层只显示一行。
            if self._on_error is not None:
                self._on_error(traceback.format_exc())
            await s.show_message(f"error: {e}")
            return
        if result is None:
            await s.show_message(f"unknown command: {cmd}")
            return
        if self._on_result is not None:
            outcome = self._on_result(s, result)
            # 允许回调用同步或异步两种形式，调用方不必为了适配而包一层
            if inspect.isawaitable(outcome):
                await outcome
        if result.handler is not None:
            s.set_handler(result.handler)
