"""命令规格与注册装饰器。

命令就是一个异步函数：``(services, args) -> CommandResult``。
``services`` 是引擎对外的能力集合（见 :mod:`lls.session.services`）——
命令直接用它做事，不需要再套一层「上下文」或「UI 协议」。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from lls.commands.result import CommandResult
from lls.session.services import Services

__all__ = ["CommandFn", "CommandSpec", "all_commands", "command", "get_command"]

# 赋值而非注解：导入时求值，``from __future__ import annotations`` 不覆盖它，
# 因此这里写 Optional 而不是 ``str | None``（后者要 3.10）。
CommandFn = Callable[[Services, Optional[str]], Awaitable[CommandResult]]


@dataclass
class CommandSpec:
    name: str
    fn: CommandFn
    aliases: tuple[str, ...] = ()
    category: str = "misc"
    summary: str = ""
    #: 行尾语法提示（含命令名，如 ``agent [任务]``）；补全与 help 共用。
    usage: str = ""
    #: 参数位允许补全的静态关键词；空表示不猜参数（键名、会话 id 都不猜）。
    subcommands: tuple[str, ...] = ()


_REGISTRY: dict[str, CommandSpec] = {}


def command(
    name: str,
    aliases: tuple[str, ...] = (),
    category: str = "misc",
    usage: str = "",
    subcommands: tuple[str, ...] = (),
) -> Callable[[CommandFn], CommandFn]:
    """把函数注册为命令。docstring 首行作为说明。

    ``usage`` 只用于行尾提示与补全（``agent [任务]``），不参与执行；
    ``subcommands`` 声明参数位可补的静态关键词。
    """

    def decorator(fn: CommandFn) -> CommandFn:
        summary = (fn.__doc__ or "").strip().split("\n", 1)[0]
        spec = CommandSpec(
            name=name,
            fn=fn,
            aliases=aliases,
            category=category,
            summary=summary,
            usage=usage or name,
            subcommands=subcommands,
        )
        _REGISTRY[name] = spec
        for alias in aliases:
            _REGISTRY[alias] = spec
        return fn

    return decorator


def get_command(name: str) -> CommandSpec | None:
    return _REGISTRY.get(name)


def all_commands() -> dict[str, CommandSpec]:
    return dict(_REGISTRY)