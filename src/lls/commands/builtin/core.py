"""核心命令：退出、清屏、错误查看、帮助。"""

from __future__ import annotations

from lls.commands.result import CommandResult
from lls.commands.spec import command
from lls.session.services import Services

__all__ = ["record_error"]

# 最近一次被捕获的错误（供 err 命令显示）。命令层自己维护，框架不参与。
_last_error: list[str] = []


def record_error(text: str) -> None:
    """记录一条异常，供 ``err`` 命令查看。"""
    _last_error.clear()
    _last_error.append(text)


@command("quit", aliases=("q", "exit"), category="core", usage="quit")
async def cmd_quit(s: Services, args: str | None) -> CommandResult:
    """退出当前模式，恢复终端显示"""
    return CommandResult(handler="default")


@command("err", category="core", usage="err")
async def cmd_err(s: Services, args: str | None) -> CommandResult:
    """显示最近捕获的错误信息"""
    if not _last_error:
        await s.show_message("没有捕获到错误")
    else:
        await s.show_message(f"最近的错误：\n{_last_error[0]}")
    return CommandResult()


@command("help", aliases=("h", "?"), category="core", usage="help")
async def cmd_help(s: Services, args: str | None) -> CommandResult:
    """列出所有可用命令"""
    from lls.commands.registry import command_help

    await s.show_message(command_help())
    return CommandResult()
