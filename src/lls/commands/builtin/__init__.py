"""内置命令 —— 命令名与别名对照 legacy，每个命令是**完整处理流程**。

设计：命令通过 ``ctx.ui`` / ``ctx.io`` 直接完成行为（清屏、显示、读输入、
写子进程），只有「改 controller 模式」和「退出程序」通过 ``CommandResult`` 返回。
详见 :mod:`lls.commands.context` 与 :mod:`lls.session.result` 的说明。

命令名与别名对照 legacy：

    q/quit/exit, s/show/status, r/raw, ch/chat, reset, c/clear,
    w/watch, t/tty, esc,
    g/gen/generate, e/exec, i/input, a/auto,
    err, conf/config/configs, set, get, m/mode,
    create, remove/del/delete, rename, l/ls
"""

from __future__ import annotations

import asyncio

from ..context import CommandContext
from ..spec import command
from lls.session.result import CommandResult


# ---- core ----

@command("quit", aliases=("q", "exit"), category="core")
async def cmd_quit(ctx: CommandContext, args: str | None) -> CommandResult:
    """退出当前模式，恢复终端显示"""
    return CommandResult(mode="char")


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
    for t in ctx.session.turns:
        lines.append(f"user: {t.instruct}")
        lines.append(f"assistant: {t.output}")
    await ctx.ui.show_message("\n".join(lines))
    return CommandResult()


@command("reset", category="core")
async def cmd_reset(ctx: CommandContext, args: str | None) -> CommandResult:
    """重置终端和屏幕状态，恢复初始模式"""
    return CommandResult(mode="char")


@command("clear", aliases=("c",), category="core")
async def cmd_clear(ctx: CommandContext, args: str | None) -> CommandResult:
    """清屏"""
    ctx.ui.clear_screen()
    return CommandResult()


@command("err", category="core")
async def cmd_err(ctx: CommandContext, args: str | None) -> CommandResult:
    """显示最近捕获的错误信息"""
    await ctx.ui.show_message("no catched error")
    return CommandResult()


@command("conf", aliases=("config", "configs"), category="core")
async def cmd_conf(ctx: CommandContext, args: str | None) -> CommandResult:
    """显示当前 AI 实例的参数配置"""
    lines = [
        f"role={ctx.role_id}",
        f"model={ctx.ai_config.model}",
        f"base_url={ctx.ai_config.base_url or '(default)'}",
        f"roles=[{','.join(ctx.roles)}]",
    ]
    await ctx.ui.show_message("\n".join(lines))
    return CommandResult()


# ---- terminal ----

@command("watch", aliases=("w",), category="terminal")
async def cmd_watch(ctx: CommandContext, args: str | None) -> CommandResult:
    """监控屏幕内容变化，定时刷新显示"""
    await ctx.ui.show_message("watch: not implemented")
    return CommandResult()


@command("tty", aliases=("t",), category="terminal")
async def cmd_tty(ctx: CommandContext, args: str | None) -> CommandResult:
    """进入原始终端显示模式"""
    return CommandResult(mode="char")


@command("esc", category="terminal")
async def cmd_esc(ctx: CommandContext, args: str | None) -> CommandResult:
    """处理终端转义序列调试"""
    await ctx.ui.show_message("esc: not implemented")
    return CommandResult()


# ---- generate ----

@command("generate", aliases=("g", "gen"), category="ai")
async def cmd_generate(ctx: CommandContext, args: str | None) -> CommandResult:
    """AI 生成一条 shell 命令"""
    return CommandResult(mode="prompt")


# ---- exec / input（完整流程在命令内）----

async def _exec_flow(
    ctx: CommandContext, *, enter: bool, data: str | None
) -> CommandResult:
    """exec / input 的共同流程（对照 legacy cmd_exec_handler + cmd_exec/cmd_input）。

    两者的差异只在写入语义：

    * exec ：``(cmd): ``，history_id='cmd'，无条件存历史（instruct 为 None 时用
      '继续' 兜底），写 shell 时**加换行**；
    * input：``(input): ``，history_id='cmd_input'，**不存历史**，**不加换行**。

    恢复画面统一走 ``restore_context``：它按「模型是否已领先于物理终端」决定
    单行恢复还是整屏重绘 —— 命令有输出就整屏重绘（结果自然呈现），无输出就
    原地恢复（如 ``cd``，与真实终端一致）。不再对 exec 单独用 show_screen：
    那是带边框与状态行的调试视图，会盖住命令输出本身。
    """
    prompt = "cmd" if enter else "input"
    history_id = "cmd" if enter else "cmd_input"

    if data is None:
        line = await ctx.io.read_line(
            f"({prompt}): ", history_id=history_id, cancel=""
        )
        if line is None:
            line = ""
    else:
        line = data

    # 解析 # 分割（命令#注释）
    instruct: str | None = None
    if "#" in line:
        parts = line.split("#")
        line = parts[0].strip()
        instruct = parts[-1].strip()

    # 清掉提示行（legacy cmd_exec_handler: print('\033[2K\r')）
    ctx.ui.clear_screen_line()

    if line:
        if enter:
            # exec 无条件存历史
            ctx.session.append(instruct or "继续", ctx.make_context(), line)
        ctx.io.write(line.encode(), enter=enter)
        await asyncio.sleep(0.1)
        ctx.ui.restore_context()
    # exec 与 input 都是「向子进程写一段内容」，执行后同样留在 line 模式。
    return CommandResult()


@command("exec", aliases=("e",), category="ai")
async def cmd_exec(ctx: CommandContext, args: str | None) -> CommandResult:
    """执行单条命令，末尾自动附加回车"""
    return await _exec_flow(ctx, enter=True, data=args)


@command("input", aliases=("i",), category="ai")
async def cmd_input(ctx: CommandContext, args: str | None) -> CommandResult:
    """执行单条命令，末尾不附加回车"""
    return await _exec_flow(ctx, enter=False, data=args)


@command("auto", aliases=("a",), category="ai")
async def cmd_auto(ctx: CommandContext, args: str | None) -> CommandResult:
    """自动 AI 生成并执行命令"""
    await ctx.ui.show_message("auto: not implemented")
    return CommandResult()


# ---- ai ----

@command("mode", aliases=("m",), category="ai")
async def cmd_mode(ctx: CommandContext, args: str | None) -> CommandResult:
    """切换当前 AI 实例"""
    ids = "[" + ",".join(ctx.roles) + "]"
    if args and args in ctx.roles:
        ctx.role_id = args
        ctx.session.role_id = args
        await ctx.ui.show_message(f"change ai to '{args}'")
    else:
        await ctx.ui.show_message(f"no such ai '{args}' {ids}")
    return CommandResult()


@command("create", category="ai")
async def cmd_create(ctx: CommandContext, args: str | None) -> CommandResult:
    """创建新的 AI 实例"""
    await ctx.ui.show_message("create: not implemented")
    return CommandResult()


@command("remove", aliases=("del", "delete"), category="ai")
async def cmd_remove(ctx: CommandContext, args: str | None) -> CommandResult:
    """删除指定 AI 实例"""
    await ctx.ui.show_message("remove: not implemented")
    return CommandResult()


@command("rename", category="ai")
async def cmd_rename(ctx: CommandContext, args: str | None) -> CommandResult:
    """重命名 AI 实例"""
    await ctx.ui.show_message("rename: not implemented")
    return CommandResult()


@command("ls", aliases=("l",), category="ai")
async def cmd_ls(ctx: CommandContext, args: str | None) -> CommandResult:
    """列出所有 AI 实例及其状态"""
    lines = ["STATUS\tID\tTYPE"]
    for rid in ctx.roles:
        mark = " [*]" if rid == ctx.role_id else " [ ]"
        lines.append(f"{mark}\t{rid}\t{ctx.roles[rid].__class__.__name__}")
    await ctx.ui.show_message("\n".join(lines))
    return CommandResult()


@command("set", category="ai")
async def cmd_set(ctx: CommandContext, args: str | None) -> CommandResult:
    """设置当前 AI 实例的配置项"""
    await ctx.ui.show_message("set: not implemented")
    return CommandResult()


@command("get", category="ai")
async def cmd_get(ctx: CommandContext, args: str | None) -> CommandResult:
    """获取当前 AI 实例的配置项"""
    if args:
        value = str(getattr(ctx.ai_config, args, "?"))
        await ctx.ui.show_message(f"{args} = {value}")
    else:
        await ctx.ui.show_message(f"model={ctx.ai_config.model}")
    return CommandResult()
