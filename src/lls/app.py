"""组合根：装配引擎、处理器与命令层并运行。

这里是唯一知道「有哪些处理器、初始用哪个」的地方。引擎本身不认识这些名字，
因此新增一种输入方式只需在 :func:`build_handlers` 里注册，无需改动引擎。
"""

from __future__ import annotations

import asyncio
import os
import sys

from .ai import Session
from .commands import CommandContext
from .handlers import CommandHandler, FlowHandler, PassthroughHandler
from .session.engine import Engine
from .session.services import Handler, Services
from .settings import Paths, _xdg_config_dir, load_settings
from .term import PyteScreenModel
from .tty import make_backend

__all__ = ["run_interactive"]

# 绑定到默认处理器的字节：Ctrl-E 进命令模式，Ctrl-G 进生成模式。
# 这些是**应用层约定**，引擎不认识它们的含义。
_KEY_COMMAND = 0x05
_KEY_GENERATE = 0x07

# 命令模式读行时按 Esc 的返回哨兵
_ESCAPE = object()


def build_handlers(
    settings_factory,
    session: Session,
    paths: Paths,
) -> dict[str, Handler]:
    """构造处理器注册表。应用层唯一需要知道名字的地方。"""

    def make_context(s: Services) -> CommandContext:
        settings = settings_factory()
        return CommandContext(
            ui=s,  # type: ignore[arg-type]
            io=s,  # type: ignore[arg-type]
            screen=s.screen,
            ai_config=settings.ai,
            roles=settings.roles,
            role_id=settings.role_id,
            session=session,
            raw=s.screen_raw(),
        )

    def on_result(ctx: CommandContext, result) -> None:
        # 命令可能改了 role_id：写回应用层配置
        settings = settings_factory()
        if ctx.role_id != settings.role_id:
            settings_factory.replace(role_id=ctx.role_id)

    def on_error(detail: str) -> None:
        # 保留完整堆栈，供 err 命令查看（覆盖层只显示一行摘要）
        from .commands.builtin.core import record_error

        record_error(detail)

    async def generate_flow(s: Services, ctx: CommandContext) -> None:
        """Ctrl-G 直接进入生成流程（等价 legacy prompt_mode）。"""
        from .commands.builtin.generate import generate_flow as run_generate

        cmd, instruct, enter = await run_generate(ctx, None)
        if cmd:
            ctx.session.append(instruct, ctx.make_context(), cmd)
            ctx.io.write(cmd.encode(), enter=enter)
            await asyncio.sleep(0.1)
            ctx.ui.restore_context()

    return {
        "default": PassthroughHandler(
            {_KEY_COMMAND: "command", _KEY_GENERATE: "generate"}
        ),
        "command": CommandHandler(
            ":",
            make_context=make_context,
            on_result=on_result,
            on_error=on_error,
            escape=_ESCAPE,
        ),
        "generate": FlowHandler(generate_flow, make_context=make_context),
    }


class _SettingsHolder:
    """持有当前配置，允许命令在运行时切换角色。"""

    def __init__(self, settings) -> None:
        self._settings = settings

    def __call__(self):
        return self._settings

    def replace(self, **kwargs) -> None:
        from dataclasses import replace

        self._settings = replace(self._settings, **kwargs)

    @property
    def role_id(self) -> str:
        return self._settings.role_id


def run_interactive(
    command: list[str], *, config: str | None = None, role: str | None = None
) -> int:
    config_dir = _xdg_config_dir()
    paths = Paths(config_dir)
    config_file = paths.config_file if config is None else config

    settings = load_settings(config_file)
    if role and role in settings.roles:
        from dataclasses import replace

        settings = replace(settings, role_id=role)

    holder = _SettingsHolder(settings)

    try:
        size = os.get_terminal_size()
    except OSError:
        size = os.terminal_size((80, 24))
    screen = PyteScreenModel(rows=size.lines, columns=size.columns)

    session = Session(id="main", role_id=settings.role_id)
    backend = make_backend()
    handlers = build_handlers(holder, session, paths)
    engine = Engine(backend, command, screen, handlers, "default")

    try:
        return asyncio.run(engine.run())
    except RuntimeError as e:
        print(f"lls: {e}", file=sys.stderr)
        return 1
