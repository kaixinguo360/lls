"""命令分发。

命令自己完成全部行为，返回值只承载「命令自己做不了」的事
（模式切换 / 退出），见 :class:`~lls.session.result.CommandResult`。
"""

from __future__ import annotations

from .context import CommandContext
from .spec import all_commands, get_command
from lls.session.result import CommandResult

__all__ = ["command_help", "execute_command"]


async def execute_command(
    name: str, ctx: CommandContext, args: str | None
) -> CommandResult | None:
    """执行命令；返回 ``CommandResult``，命令不存在时返回 None。"""
    spec = get_command(name)
    if spec is None:
        return None
    return await spec.fn(ctx, args)


def command_help() -> str:
    lines = ["可用命令："]
    seen: set[str] = set()
    for spec in all_commands().values():
        if spec.name in seen:
            continue
        seen.add(spec.name)
        aliases = ",".join(spec.aliases) if spec.aliases else ""
        suffix = f" ({aliases})" if aliases else ""
        lines.append(f"  {spec.name}{suffix} - {spec.summary}")
    return "\n".join(lines)
