"""核心命令：退出、显示、清屏、配置查看等。

对应 legacy ``commands/core.py``，但语义随新架构调整：

* 退出到默认处理器改由 ``CommandResult(handler="default")`` 表达 —— 命令
  不直接改框架状态，只提出请求；
* ``conf`` / ``chat`` 直接读应用层注入的上下文，不再依赖 AI 对象的反射接口。
"""

from __future__ import annotations

from lls.commands.context import CommandContext
from lls.commands.spec import command
from lls.commands.result import CommandResult

__all__ = [
    "cmd_chat",
    "cmd_clear",
    "cmd_conf",
    "cmd_err",
    "cmd_get",
    "cmd_ls",
    "cmd_mode",
    "cmd_quit",
    "cmd_raw",
    "cmd_reset",
    "cmd_show",
]

# 最近一次被捕获的错误（供 err 命令显示）。
# 命令层自己维护，框架不参与。
_last_error: list[str] = []


def record_error(text: str) -> None:
    """记录一条异常，供 ``err`` 命令查看。"""
    _last_error.clear()
    _last_error.append(text)


@command("quit", aliases=("q", "exit"), category="core")
async def cmd_quit(ctx: CommandContext, args: str | None) -> CommandResult:
    """退出当前模式，恢复终端显示"""
    return CommandResult(handler="default")


@command("show", aliases=("s", "status"), category="core")
async def cmd_show(ctx: CommandContext, args: str | None) -> CommandResult:
    """清屏并完美显示当前屏幕内容"""
    ctx.ui.show_screen()
    return CommandResult()


@command("raw", aliases=("r",), category="core")
async def cmd_raw(ctx: CommandContext, args: str | None) -> CommandResult:
    """清屏并以原始格式输出当前屏幕内容"""
    ctx.ui.show_screen(raw=True)
    return CommandResult()


@command("chat", aliases=("ch",), category="core")
async def cmd_chat(ctx: CommandContext, args: str | None) -> CommandResult:
    """打印对话状态信息"""
    if not ctx.session.turns:
        await ctx.ui.show_message("(no chat history)")
        return CommandResult()
    lines: list[str] = []
    for turn in ctx.session.turns:
        lines.append(f"user: {turn.instruct}")
        lines.append(f"assistant: {turn.output}")
    await ctx.ui.show_message("\n".join(lines))
    return CommandResult()


@command("reset", category="core")
async def cmd_reset(ctx: CommandContext, args: str | None) -> CommandResult:
    """重置终端和屏幕状态，恢复初始模式"""
    ctx.ui.clear_screen()
    return CommandResult(handler="default")


@command("clear", aliases=("c",), category="core")
async def cmd_clear(ctx: CommandContext, args: str | None) -> CommandResult:
    """清屏"""
    ctx.ui.clear_screen()
    return CommandResult()


@command("err", category="core")
async def cmd_err(ctx: CommandContext, args: str | None) -> CommandResult:
    """显示最近捕获的错误信息"""
    ctx.ui.clear_screen()
    if not _last_error:
        await ctx.ui.show_message("no catched error")
    else:
        await ctx.ui.show_message(f"catched error:\n{_last_error[0]}")
    return CommandResult()


@command("conf", aliases=("config", "configs"), category="core")
async def cmd_conf(ctx: CommandContext, args: str | None) -> CommandResult:
    """显示当前 AI 实例的参数配置"""
    lines = [
        f"(str) model = {ctx.ai_config.model}",
        f"(str) base_url = {ctx.ai_config.base_url or ''}",
        f"(str) api_key_env = {ctx.ai_config.api_key_env}",
        f"(float) timeout = {ctx.ai_config.timeout}",
        f"(str) role = {ctx.role_id}",
        f"(list) roles = {','.join(ctx.roles)}",
    ]
    await ctx.ui.show_message("\n".join(lines))
    return CommandResult()


@command("mode", aliases=("m",), category="ai")
async def cmd_mode(ctx: CommandContext, args: str | None) -> CommandResult:
    """切换当前 AI 角色（不带参数时交互式选择）"""
    ids = "[" + ",".join(ctx.roles) + "]"
    target = args
    if not target:
        # 无参数：与 legacy 一致，提示当前值并读一行
        info = f"(select-ai) current ai is '{ctx.role_id}' {ids} "
        target = await ctx.io.read_line(info, cancel="", include_last=False)
        if not target:
            return CommandResult()
    target = target.strip()
    if target in ctx.roles:
        ctx.role_id = target
        ctx.session.role_id = target
        await ctx.ui.show_message(f"change ai to '{target}'")
    else:
        await ctx.ui.show_message(f"no such ai '{target}' {ids}")
    return CommandResult()


@command("ls", aliases=("l",), category="ai")
async def cmd_ls(ctx: CommandContext, args: str | None) -> CommandResult:
    """列出所有 AI 角色及当前选择"""
    lines = ["STATUS\tID\tMODEL"]
    for rid in ctx.roles:
        role = ctx.roles[rid]
        mark = " [*]" if rid == ctx.role_id else " [ ]"
        lines.append(f"{mark}\t{rid}\t{role.model or '(default)'}")
    await ctx.ui.show_message("\n".join(lines))
    return CommandResult()


@command("get", category="ai")
async def cmd_get(ctx: CommandContext, args: str | None) -> CommandResult:
    """查看配置项的值（不带参数时列出全部）"""
    if not args:
        return await cmd_conf(ctx, None)
    key = args.strip()
    value = getattr(ctx.ai_config, key, None)
    if value is None:
        await ctx.ui.show_message(f"no such config '{key}'")
    else:
        text = str(value).replace("\n", "\\n")
        await ctx.ui.show_message(f"{key} = {text}")
    return CommandResult()
