"""命令规格与注册装饰器。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable

from .context import CommandContext
from lls.session.result import CommandResult

__all__ = ["CommandSpec", "command"]

CommandFn = Callable[[CommandContext, str | None], Awaitable[CommandResult]]


@dataclass(slots=True)
class CommandSpec:
    name: str
    fn: CommandFn
    aliases: tuple[str, ...] = ()
    category: str = "misc"
    summary: str = ""


_REGISTRY: dict[str, CommandSpec] = {}


def command(
    name: str,
    aliases: tuple[str, ...] = (),
    category: str = "misc",
) -> Callable[[CommandFn], CommandFn]:
    """把函数注册为命令。docstring 首行作为说明。"""

    def decorator(fn: CommandFn) -> CommandFn:
        summary = (fn.__doc__ or "").strip().split("\n", 1)[0]
        spec = CommandSpec(
            name=name, fn=fn, aliases=aliases, category=category, summary=summary
        )
        _REGISTRY[name] = spec
        for a in aliases:
            _REGISTRY[a] = spec
        return fn

    return decorator


def get_command(name: str) -> CommandSpec | None:
    return _REGISTRY.get(name)


def all_commands() -> dict[str, CommandSpec]:
    return dict(_REGISTRY)
