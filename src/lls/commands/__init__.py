"""命令层：上下文、规格、注册表、分发、内置命令。"""

from .context import CommandContext, ShellService, UIService
from .registry import command_help, execute_command
from .spec import CommandSpec, all_commands, command, get_command
from lls.session.result import CommandResult

# 导入内置命令以触发注册
from . import builtin  # noqa: F401

__all__ = [
    "CommandContext",
    "CommandResult",
    "CommandSpec",
    "ShellService",
    "UIService",
    "all_commands",
    "command",
    "command_help",
    "execute_command",
    "get_command",
]
