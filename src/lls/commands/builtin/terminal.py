"""终端相关命令：watch / tty。

两者都建立在框架原语之上，不含任何框架预设：

* ``tty`` —— 隐藏光标，持续把屏幕内容以原始字节重画，逐字节转发输入，
  直到收到 Ctrl-E。它用 ``call_later`` 轮询而不是 ``SIGALRM``：定时器在
  事件循环里比信号更安全，也不会与引擎自己的信号处理器冲突。
* ``watch`` —— 定时刷新屏幕，同时接受单键操作，按键可触发生成或执行。
  变化判定用屏幕文本快照比对（``dirty`` 是渲染用的、会被清空，不能当
  累积计数器）。
"""

from __future__ import annotations

import asyncio
import time

from lls.commands.context import CommandContext
from lls.commands.spec import command
from lls.commands.result import CommandResult

__all__ = ["cmd_tty", "cmd_watch"]

# watch 的刷新周期（秒）。与 legacy 的 2 秒一致。
_WATCH_INTERVAL = 2.0

# tty 的轮询间隔（秒）：等待按键的同时，靠超时触发一次画面刷新。
_TTY_POLL = 0.2


@command("tty", aliases=("t",), category="terminal")
async def cmd_tty(ctx: CommandContext, args: str | None) -> CommandResult:
    """以原始终端模式持续刷新屏幕，Ctrl-E 退出"""
    hide = b"\x1b[?25l"
    show = b"\x1b[?25h"
    write_terminal = getattr(ctx.ui, "write_terminal", None)
    if write_terminal is not None:
        write_terminal(hide)
    try:
        ctx.ui.show_screen(raw=True)
        while ctx.io.child_alive:
            # 带超时地读输入：超时即刷新一帧，这样即使没有按键，
            # 子进程的新输出也能及时显示出来（legacy 靠回调做到这一点）。
            data = await ctx.io.read_chars(_TTY_POLL)
            if data is None:
                break
            if data:
                stop = False
                for i, byte in enumerate(data):
                    if byte == 0x05:  # Ctrl-E
                        if i:
                            ctx.io.write(data[:i])
                        stop = True
                        break
                    ctx.io.write(bytes([byte]))
                if stop:
                    break
            ctx.ui.show_screen(raw=True)
    finally:
        if write_terminal is not None:
            write_terminal(show)
        ctx.ui.restore_context()
    return CommandResult(handler="command")


@command("watch", aliases=("w",), category="terminal")
async def cmd_watch(ctx: CommandContext, args: str | None) -> CommandResult:
    """定时监控屏幕变化，可按键生成或执行命令"""
    services = ctx.ui
    tick = getattr(services, "call_later", None)

    last_snapshot = [""]
    timer: list[object] = [None]
    running = [True]

    def refresh() -> None:
        """内容变化时重画屏幕，并刷新底部的「每 2 秒」时间条。"""
        if not running[0]:
            return
        snapshot = ctx.screen.text()
        if snapshot != last_snapshot[0]:
            last_snapshot[0] = snapshot
            ctx.ui.show_screen()
        stamp = time.asctime(time.localtime(time.time()))
        # 覆盖上一行再写时间条，避免每 2 秒把终端越撑越长
        bar = f"\x1b[1A\x1b[2K\rEvery {_WATCH_INTERVAL:.1f}s: show\t{stamp}"
        write_terminal = getattr(ctx.ui, "write_terminal", None)
        if write_terminal is not None:
            write_terminal(b"\r\n" + bar.encode() + b"\r\n")
        schedule()

    def schedule() -> None:
        if tick is None or not running[0]:
            return
        timer[0] = tick(_WATCH_INTERVAL, refresh)

    # 立即画一帧，随后进入按键循环
    refresh()

    try:
        while True:
            key = await ctx.io.read_key()
            if key is None:
                break
            stop, action = _watch_key(key)
            if stop:
                break
            if action == "generate":
                await _watch_generate(ctx)
            elif action == "exec":
                await _watch_exec(ctx, enter=True)
            elif action == "input":
                await _watch_exec(ctx, enter=False)
            elif action is not None:
                ctx.io.write(action)
                await asyncio.sleep(0.1)
            refresh()
    finally:
        running[0] = False
        if tick is not None and timer[0] is not None:
            cancel = getattr(services, "cancel_timer", None)
            if cancel is not None:
                cancel(timer[0])
        ctx.ui.restore_context()
    return CommandResult(handler="command")


def _watch_key(key) -> tuple[bool, object]:
    """把按键翻译成 ``(是否退出, 动作)``。

    动作是 ``"generate"`` / ``"exec"`` / ``"input"``，或要直接写给子进程的字节，
    或 None 表示忽略。
    """
    if key.control in ("ctrl_c", "ctrl_d"):
        return True, None
    char = key.char or ""
    if char == "q":
        return True, None
    if char == "g":
        return False, "generate"
    if char == "e":
        return False, "exec"
    if char == "i":
        return False, "input"
    if char == "b":
        return False, b"\b"
    if char == "n":
        return False, b"\n"
    if char == "c":
        return False, b"\x03"
    if char == "d":
        return False, b"\x04"
    return False, None


async def _watch_generate(ctx: CommandContext) -> None:
    """watch 里按 g：跑一遍生成流程并执行结果。"""
    from .generate import generate_flow

    cmd, instruct, enter = await generate_flow(ctx, None)
    if cmd:
        ctx.session.append(instruct, ctx.make_context(), cmd)
        ctx.io.write(cmd.encode(), enter=enter)
        await asyncio.sleep(0.1)


async def _watch_exec(ctx: CommandContext, *, enter: bool) -> None:
    """watch 里按 e / i：读一行写入子进程。"""
    prompt = "cmd" if enter else "input"
    history_id = "cmd" if enter else "cmd_input"
    line = await ctx.io.read_line(f"({prompt}): ", history_id=history_id, cancel="")
    if not line:
        return
    body, instruct = _split_instruct(line)
    if not body:
        return
    if enter:
        ctx.session.append(instruct or "继续", ctx.make_context(), body)
    ctx.io.write(body.encode(), enter=enter)
    await asyncio.sleep(0.1)


def _split_instruct(line: str) -> tuple[str, str | None]:
    """按 ``#`` 拆成 ``(命令, 指令)``。"""
    if "#" not in line:
        return line.strip(), None
    head, _, tail = line.partition("#")
    return head.strip(), tail.strip() or None
