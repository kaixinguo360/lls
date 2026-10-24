"""向子进程写入内容的命令：exec / input。

两者的差异只在写入语义：

* ``exec``  —— 提示 ``(cmd): ``，历史 id 为 ``cmd``，**无条件存历史**
  （指令为空时用「继续」兜底），写子进程时**加换行**；
* ``input`` —— 提示 ``(input): ``，历史 id 为 ``cmd_input``，**不存历史**，
  **不加换行**。

两者执行后都留在当前处理器（与 legacy 一致：都是「向子进程写一段内容」，
不改变所处的输入模式）。
"""

from __future__ import annotations

import asyncio

from lls.commands.context import CommandContext
from lls.commands.spec import command
from lls.commands.result import CommandResult

__all__ = ["cmd_exec", "cmd_input"]


def _split_instruct(line: str) -> tuple[str, str | None]:
    """按 ``#`` 拆成 ``(命令, 指令)``。

    与 legacy 一致：``#`` 前是命令本体，``#`` 后是给它配的指令说明。
    """
    if "#" not in line:
        return line.strip(), None
    head, _, tail = line.partition("#")
    return head.strip(), tail.strip() or None


async def _write_flow(
    ctx: CommandContext, *, enter: bool, data: str | None
) -> CommandResult:
    prompt = "cmd" if enter else "input"
    history_id = "cmd" if enter else "cmd_input"

    if data is None:
        line = await ctx.io.read_line(
            f"({prompt}): ", history_id=history_id, cancel=""
        )
        line = line or ""
    else:
        line = data

    body, instruct = _split_instruct(line)
    if not body:
        return CommandResult()

    if enter:
        # exec 无条件存历史：指令缺失时用「继续」，与 legacy 一致
        ctx.session.append(instruct or "继续", ctx.make_context(), body)
    ctx.io.write(body.encode(), enter=enter)
    await asyncio.sleep(0.1)
    ctx.ui.restore_context()
    return CommandResult()


@command("exec", aliases=("e",), category="ai")
async def cmd_exec(ctx: CommandContext, args: str | None) -> CommandResult:
    """执行单条命令，末尾自动附加回车"""
    return await _write_flow(ctx, enter=True, data=args)


@command("input", aliases=("i",), category="ai")
async def cmd_input(ctx: CommandContext, args: str | None) -> CommandResult:
    """执行单条命令，末尾不附加回车"""
    return await _write_flow(ctx, enter=False, data=args)
