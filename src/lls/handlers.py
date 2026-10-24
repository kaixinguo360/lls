"""应用层输入处理器 —— 框架之外的一层。

引擎只反复调用 ``step()``，因此「怎样响应输入」完全在这里决定。本模块给出
三个通用处理器：

* :class:`PassthroughHandler` —— 子进程独占终端，输入原样转发（可绑定
  某些字节切到别的处理器）；
* :class:`CommandHandler` —— 读一行，按注册表分发为命令；
* :class:`FlowHandler` —— 执行一段自定义异步流程后自动切回。

三者都不包含具体业务：命令逻辑在 :mod:`lls.commands` 里，本层只做分发与
流程托管。新增一种输入方式只需组一个新的处理器，不必改动引擎。
"""

from __future__ import annotations

import inspect
import traceback
from typing import Awaitable, Callable

from lls.commands import CommandContext, execute_command
from lls.commands.result import CommandResult
from lls.session.services import Services

__all__ = [
    "CommandHandler",
    "FlowHandler",
    "PassthroughHandler",
    "split_command",
]


def split_command(line: str) -> tuple[str, str | None]:
    """把 ``"name args..."`` 拆成 ``(name, args)``；args 为空时为 None。"""
    line = line.strip()
    if " " in line:
        cmd, _, args = line.partition(" ")
        return cmd.strip(), args.strip() or None
    return line, None


class FlowHandler:
    """执行一段自定义异步流程，结束后切回指定处理器。

    ``flow`` 收到 ``(services, context)``，可自由读写终端与子进程。本处理器
    不做任何业务判断 —— 流程内容完全由调用方决定。这样「一次性动作」
    （生成一条命令、跑一段交互）不必写成一个 Python 类，也不进命令注册表。
    """

    passthrough = False

    def __init__(
        self,
        flow: Callable[[Services, CommandContext], Awaitable[None]],
        *,
        make_context: Callable[[Services], CommandContext] | None = None,
        next_handler: str = "default",
    ) -> None:
        self._flow = flow
        self._make_context = make_context
        self._next = next_handler

    async def step(self, s: Services) -> None:
        ctx = (
            self._make_context(s)
            if self._make_context is not None
            else _plain_context(s)
        )
        try:
            await self._flow(s, ctx)
        finally:
            s.set_handler(self._next)


class PassthroughHandler:
    """子进程独占终端：输入原样转发，只拦截绑定的字节。

    ``bindings`` 把字节值映射到处理器名，例如 ``{0x05: "command"}``。
    引擎只认处理器名，不认识这几个字节的含义 —— 那是应用层的约定。
    ``on_exit`` 在子进程退出、处理器收到 ``None`` 时调用：可用来收尾
    （例如把终端内容留给用户看）。
    """

    passthrough = True

    def __init__(
        self,
        bindings: dict[int, str] | None = None,
        *,
        on_exit: Callable[[Services], Awaitable[None]] | None = None,
    ) -> None:
        self._bindings = dict(bindings or {})
        self._on_exit = on_exit

    async def step(self, s: Services) -> None:
        data = await s.read_chars()
        if data is None:
            # 子进程已退出
            if self._on_exit is not None:
                await self._on_exit(s)
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

    命令自己做完全部行为，只有「切处理器」和「退出」通过 ``CommandResult``
    返回 —— 那两件事需要引擎配合。

    ``make_context`` 负责把 ``Services`` 包装成命令需要的上下文（注入
    AI 配置、会话等应用层数据）；省略时构造一个只含屏幕与能力的最小上下文。
    """

    passthrough = False

    def __init__(
        self,
        prompt: str = ":",
        *,
        make_context: Callable[[Services], CommandContext] | None = None,
        on_result: Callable[[CommandContext, CommandResult], Awaitable[None]]
        | None = None,
        on_error: Callable[[str], None] | None = None,
        cancel: str | None = "q",
        escape: object | None = None,
        restore_on_exit: bool = True,
    ) -> None:
        self._prompt = prompt
        self._cancel = cancel
        self._escape = escape
        self._make_context = make_context
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
        ctx = (
            self._make_context(s)
            if self._make_context is not None
            else _plain_context(s)
        )
        try:
            result = await execute_command(cmd, ctx, args)
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
            outcome = self._on_result(ctx, result)
            # 允许回调用同步或异步两种形式，调用方不必为了适配而包一层
            if inspect.isawaitable(outcome):
                await outcome
        if result.quit:
            # 请求退出整个程序
            s.request_quit()
        elif result.handler is not None:
            s.set_handler(result.handler)


def _plain_context(s: Services) -> CommandContext:
    """没有外部应用数据时构造一个最小上下文（仅屏幕与能力）。"""
    from lls.ai import AIConfig, Session
    from lls.ai.roles import DEFAULT_ROLES

    return CommandContext(
        ui=s,  # type: ignore[arg-type]
        io=s,  # type: ignore[arg-type]
        screen=s.screen,
        ai_config=AIConfig(),
        roles=dict(DEFAULT_ROLES),
        role_id="chat",
        session=Session(id="main", role_id="chat"),
    )
