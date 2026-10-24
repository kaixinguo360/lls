"""向子进程写入内容的命令：exec / input。

两者的差异只在写入语义：

* ``exec``  —— 提示 ``(cmd): ``，历史 id 为 ``cmd``，写子进程时**加换行**；
* ``input`` —— 提示 ``(input): ``，历史 id 为 ``cmd_input``，**不加换行**。

两者执行后都留在当前处理器。
"""

from __future__ import annotations

import asyncio

from lls.commands.result import CommandResult
from lls.commands.spec import command
from lls.session.services import Services

__all__ = ["cmd_exec", "cmd_input"]


async def _write_flow(
    s: Services, *, enter: bool, data: str | None
) -> CommandResult:
    prompt = "cmd" if enter else "input"
    history_id = "cmd" if enter else "cmd_input"

    if data is None:
        line = await s.read_line(f"({prompt}): ", history_id=history_id, cancel="")
        line = line or ""
    else:
        line = data

    body = line.strip()
    if not body:
        return CommandResult()

    s.write(body.encode(), enter=enter)
    await asyncio.sleep(0.1)
    s.restore_context()
    return CommandResult()


@command("exec", aliases=("e",), category="write")
async def cmd_exec(s: Services, args: str | None) -> CommandResult:
    """执行单条命令，末尾自动附加回车"""
    return await _write_flow(s, enter=True, data=args)


@command("input", aliases=("i",), category="write")
async def cmd_input(s: Services, args: str | None) -> CommandResult:
    """执行单条命令，末尾不附加回车"""
    return await _write_flow(s, enter=False, data=args)
