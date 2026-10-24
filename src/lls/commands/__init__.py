"""命令层：规格、注册表、分发、内置命令。

命令的签名是 ``(services, args) -> CommandResult``。``Services`` 由引擎提供，
命令层不定义任何 UI/IO 抽象层。
"""

from __future__ import annotations

from lls.commands.result import CommandResult

from . import builtin  # noqa: F401  # 导入以触发注册
from .registry import command_help, execute_command
from .spec import CommandSpec, all_commands, command, get_command

__all__ = [
    "CommandResult",
    "CommandSpec",
    "all_commands",
    "command",
    "command_help",
    "execute_command",
    "get_command",
]
