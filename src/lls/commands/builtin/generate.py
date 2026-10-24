"""AI 生成相关命令：generate / auto。

**生成流程在这里，不在框架里。** 这与其它命令一致：命令是完整流程，自己
通过 ``ctx`` 完成全部行为。框架只提供原语（覆盖层、读行、写子进程），完全
不知道「generate」这个概念。

正因为流程被抽成了普通函数 :func:`generate_flow`，``auto`` 与 ``watch`` 的
g 键才能直接复用同一份逻辑，而不必复制一遍。
"""

from __future__ import annotations

import asyncio
import time

from lls.ai.types import ChunkKind
from lls.commands.context import CommandContext
from lls.commands.spec import command
from lls.commands.result import CommandResult

__all__ = ["cmd_auto", "cmd_generate", "generate_flow"]

# 确认菜单的按键说明（与 legacy cmd_generate 一致）
_FLAGS = "[y/u/i/n/e/s/r/k/t]"


def _record(ctx: CommandContext, value: str, history_id: str) -> None:
    """把一条输入记入历史，供上下键回溯。"""
    recorder = getattr(ctx.io, "record_history", None)
    if recorder is not None:
        recorder(value, history_id)


async def _read_instruct(
    ctx: CommandContext, prompt: str, value: str = ""
) -> str | None:
    """读指令，支持 / 命令切换。

    以 ``/`` 开头时按子命令处理，而不是当作指令内容：

    * ``/s`` / ``/show`` / ``/status`` —— 显示屏幕
    * ``/m`` / ``/mode`` —— 查看或切换当前角色
    * ``/c`` / ``/chat`` —— 切到 chat 角色并继续
    * ``/t`` / ``/text`` —— 切到 completion 角色并继续

    返回 ``None`` 表示用户要求中止（Ctrl-C 或空输入）。
    """
    instruct: str | None = None
    while instruct is None:
        raw = await ctx.io.read_line(
            f"({prompt}-instruct): ",
            include_last=False,
            value=value,
            history_id="instruct",
        )
        value = raw or ""
        instruct = (raw or "").strip()
        if instruct[:1] != "/":
            break
        instruct = None
        value = ""
        body = (raw or "").strip()[1:]
        if " " in body and body[:1] != " ":
            head, _, rest = body.partition(" ")
            sub, args = head.strip(), rest.strip()
        else:
            sub, args = body, None
        if sub in ("s", "show", "status"):
            ctx.ui.show_screen()
        elif sub in ("m", "mode"):
            await ctx.ui.show_message(f"mode: {args or ctx.role_id}")
        elif sub in ("c", "ch", "chat"):
            instruct = args
            ctx.role_id = "chat"
        elif sub in ("t", "text"):
            instruct = args
            ctx.role_id = "completion"
    return instruct


async def generate_flow(
    ctx: CommandContext,
    instruct: str | None = None,
    *,
    prompt: str = "gen",
) -> tuple[str, str, bool]:
    """完整的「生成 + 确认」流程。

    返回 ``(cmd, instruct, enter)``：

    * ``cmd`` —— 最终要写入子进程的命令（空串表示放弃）；
    * ``instruct`` —— 最终使用的指令（供调用方存历史）；
    * ``enter`` —— 是否应追加换行（``u`` 为 True，``i`` 为 False）。

    这是 legacy ``cmd_generate`` 的等价物，但**是一个普通异步函数**，
    因此 ``auto`` 可以反复调用它，``watch`` 的 g 键也可以直接用它。
    """
    default = "u"
    if instruct is None:
        instruct = await _read_instruct(ctx, prompt)
    else:
        _record(ctx, instruct, "instruct")

    if not instruct:
        return "", "", True

    context = ctx.make_context()
    role = _resolve_role(ctx)
    cmd, think = "", ""

    # 指令里带 # 时，后面那段直接当命令用，跳过 AI
    if "#" in instruct:
        head, _, tail = instruct.partition("#")
        instruct = head.strip()
        cmd = tail.strip()
        output = None
    else:
        output = _stream(ctx, instruct, context, role)

    confirm_info = ", confirm?"
    show_think = False
    gen_time = time.time()
    enter = True

    while True:
        if output is not None:
            cmd, think = await _consume_stream(ctx, output)
            gen_time = time.time()
            output = None

        if cmd:
            _record(ctx, cmd, "cmd")

        flags_text = _FLAGS.replace(default, default.upper())
        text = f"({prompt}-cmd): {cmd}{confirm_info} {flags_text} "
        if show_think:
            text = f"({prompt}-think): {think}\n{text}"
        show_think = False
        confirm_info = ", confirm?"

        answer = await ctx.io.read_line(
            text, include_last=False, cancel="cancel", exit="n"
        )
        answer = (answer or "").lower()
        if answer == "":
            answer = default

        if answer in ("y", "yes"):
            break
        if answer in ("u", "use"):
            break
        if answer in ("i", "input"):
            enter = False
            break
        if answer in ("n", "no", "q", "quit", "exit"):
            cmd = ""
            break
        if answer in ("k", "think"):
            show_think = True
        elif answer in ("r", "re", "retry"):
            output = _stream(ctx, instruct, context, role)
        elif answer in ("e", "edit"):
            instruct = await _read_instruct(ctx, prompt, instruct)
            if not instruct:
                cmd = ""
                break
            output = _stream(ctx, instruct, context, role)
        elif answer in ("t", "teach"):
            default = "y"
            cmd = (
                await ctx.io.read_line(
                    f"({prompt}-cmd): ", include_last=False, history_id="cmd"
                )
                or ""
            )
            if cmd == "":
                break
        elif answer in ("s", "show", "status"):
            ctx.ui.show_screen()
        elif answer == "cancel":
            # 0.6s 内连续两次 cancel 才真正取消，避免误触
            if time.time() - gen_time > 0.6:
                cmd = ""
                break
            gen_time = time.time()
        else:
            confirm_info = ", please input 'y' or 'n':"

    return cmd, instruct, enter


def _resolve_role(ctx: CommandContext):
    """取当前角色；缺失时退回默认 chat。"""
    from lls.ai import get_role

    return get_role(getattr(ctx, "role_id", "chat"), getattr(ctx, "roles", None))


def _stream(ctx: CommandContext, instruct, context, role):
    """发起一次流式生成。抽成函数便于重试时复用。"""
    from lls.ai import stream_chat

    messages = ctx.session.replay(role, instruct, context)
    return stream_chat(ctx.ai_config, messages)


async def _consume_stream(ctx: CommandContext, output) -> tuple[str, str]:
    """消费流式输出，实时把内容画在覆盖层上。返回 ``(cmd, think)``。"""
    ctx.ui.draw_overlay("(gen-cmd): waiting...")
    gen_cmd, gen_think = "", ""
    cancelled = False
    try:
        async for chunk in output:
            if chunk.kind == ChunkKind.TEXT:
                gen_cmd += chunk.text
                text = f"(gen-cmd): {gen_cmd}"
            elif chunk.kind == ChunkKind.THINK:
                gen_think += chunk.text
                text = f"(gen-think): {gen_think}"
            else:
                text = "(gen-cmd): waiting..."
            ctx.ui.draw_overlay(text)
    except asyncio.CancelledError:
        cancelled = True
    finally:
        # 撤掉流式帧，并把它占的行按模型还原
        ctx.ui.restore_context()

    if not cancelled:
        return gen_cmd, gen_think
    # 被取消：若只拿到思维链，就把它当作命令
    if gen_cmd == "" and gen_think != "":
        return gen_think, ""
    return gen_cmd, gen_think


@command("generate", aliases=("g", "gen"), category="ai")
async def cmd_generate(ctx: CommandContext, args: str | None) -> CommandResult:
    """AI 生成一条 shell 命令并可直接执行"""
    cmd, _instruct, enter = await generate_flow(ctx, args)
    if cmd:
        ctx.session.append(_instruct, ctx.make_context(), cmd)
        ctx.io.write(cmd.encode(), enter=enter)
        await asyncio.sleep(0.1)
        ctx.ui.restore_context()
    return CommandResult(handler="command")


@command("auto", aliases=("a",), category="ai")
async def cmd_auto(ctx: CommandContext, args: str | None) -> CommandResult:
    """自动反复生成并执行命令，直到模型不再产出"""
    instruct = args
    if instruct is None:
        instruct = await _read_instruct(ctx, "auto")
        if not instruct:
            return CommandResult(handler="command")
    else:
        _record(ctx, instruct, "auto-instruct")

    while instruct:
        # 复用同一份生成流程，不再复制一遍
        cmd, instruct, _enter = await generate_flow(ctx, instruct)
        body = cmd.rstrip("\n") if cmd else ""
        if not body:
            break
        ctx.io.write(body.encode(), enter=True)
        await asyncio.sleep(0.1)
        ctx.ui.restore_context()
    return CommandResult(handler="command")
