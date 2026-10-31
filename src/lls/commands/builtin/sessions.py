"""会话管理命令：列、分叉、删除。

全部走 :class:`lls.ai.sessions.SessionStore`：``sessions`` 不写盘，
``fork`` / ``rm`` 明确要改盘。
"""

from __future__ import annotations

import time

from lls.ai.sessions import SessionError, store
from lls.commands.result import CommandResult
from lls.commands.spec import command
from lls.session.services import Services

__all__ = ["cmd_sessions"]


def _stamp(value: float) -> str:
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(value))
    except (OverflowError, OSError, ValueError):
        return "-"


@command("sessions", aliases=("ls",), category="ai", usage="sessions [--all]")
async def cmd_sessions(s: Services, args: str | None) -> CommandResult:
    """列出会话：id、标题、模型、轮数、大小、更新时间"""
    include_all = bool(args and "--all" in args.split())
    keeper = store()
    metas = keeper.list(include_archived=include_all)
    if not metas:
        await s.show_message("还没有会话；直接运行 agent 会新开一条")
        return CommandResult()
    current = keeper.current_id()
    rows = ["会话（* 为当前）："]
    for meta in metas:
        rows.append(
            "{} {}  {}  {}  {} 轮  {:.0f}KB  {}".format(
                "*" if meta.id == current else " ",
                meta.id,
                (meta.title or "（无标题）")[:28],
                meta.model or "-",
                meta.turns,
                meta.size / 1024.0,
                _stamp(meta.updated_at),
            )
        )
    if not include_all:
        rows.append("（--all 含已归档）")
    await s.show_message("\n".join(rows))
    return CommandResult()


@command("fork", category="ai", usage="fork <id> [--at <seq>]")
async def cmd_fork(s: Services, args: str | None) -> CommandResult:
    """从某条会话的某个位置分叉出新会话：fork <id> [--at <seq>]"""
    parts = (args or "").split()
    spec = None
    at_seq = 0
    index = 0
    while index < len(parts):
        part = parts[index]
        if part == "--at":
            index += 1
            if index >= len(parts) or not parts[index].isdigit():
                await s.show_message("--at 需要一个序号")
                return CommandResult()
            at_seq = int(parts[index])
        elif part.startswith("--"):
            await s.show_message("未知参数 {}".format(part))
            return CommandResult()
        elif spec is None:
            spec = part
        index += 1
    if spec is None:
        await s.show_message("用法：fork <id> [--at <seq>]")
        return CommandResult()
    keeper = store()
    try:
        session = keeper.fork(spec, at_seq)
    except SessionError as e:
        await s.show_message("error: {}".format(e))
        return CommandResult()
    await s.show_message(
        "已分叉到 {}（来自 {}，到 seq {}）".format(session.id, spec, at_seq or "末尾")
    )
    return CommandResult()


@command("rm", category="ai", usage="rm <id>")
async def cmd_rm(s: Services, args: str | None) -> CommandResult:
    """删除一条会话（连同它的输入历史）"""
    spec = (args or "").strip()
    if not spec:
        await s.show_message("用法：rm <id>")
        return CommandResult()
    keeper = store()
    try:
        meta = keeper.drop(spec)
    except SessionError as e:
        await s.show_message("error: {}".format(e))
        return CommandResult()
    await s.show_message("已删除会话 {}".format(meta.id))
    return CommandResult()
