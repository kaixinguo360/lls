"""Controller —— 主循环与模态调度（asyncio 单线程）。

交互模型（对照 legacy legacy/lls.py + legacy/commands/*）：

* **char 模式透传**：子进程输出直接写宿主终端（光标由子进程管理），同时旁路
  喂屏幕模型；``line``/``prompt`` 模式**不透传**，避免干扰 ``read_line`` 的显示；
* ``read_line`` 在当前光标位置用 ``\\033[2K\\r`` 清行显示，光标靠「上移 + 写前缀」
  定位（见 ``ui.renderer.LinePrinter``）；
* 提示符：行模式 ``:``、指令 ``(gen-instruct): ``、生成 ``(gen-cmd): ``、
  think ``(gen-think): ``、确认 ``(gen-cmd): {cmd}, confirm? [y/U/i/n/e/s/r/k/t] ``。

generate 确认菜单（精确还原 legacy cmd_generate）：default = ``u``；
y/yes 存历史并执行，u/use 执行，i/input 只回填不回车，n/no/q/quit/exit 放弃，
k/think 看思维链，r/re/retry 重生成，e/edit 改指令，t/teach 手敲，
s/show/status 看屏，cancel 心跳防误触（0.6s 内连续 cancel 才真取消）。

命令模型：命令是**完整的处理流程**，自己通过注入的 ``ui`` / ``io`` 能力完成行为；
只有「改 controller 模式」和「退出」通过 ``CommandResult`` 返回。

子进程退出处理：

* master 读 EIO（Linux PTY 挂断）或 EOF 会调用 ``_on_child_exit``，其中
  ``remove_reader`` 并置 ``_exit_event``；所有等输入的协程同时等 ``_exit_event``；
* SIGCHLD 兜底，主动 ``waitpid`` 回收子进程。
"""

from __future__ import annotations

import asyncio
import errno
import os
import signal
import sys
import time
from typing import Any

from lls.ai import AIError, Session, stream_chat
from lls.ai.types import ChunkKind
from lls.commands import CommandContext, execute_command
from lls.edit import LineEditor
from lls.term import ScreenModel
from lls.tty import Console, Key, KeyParser, TerminalBackend, Winsize
from lls.ui import LinePrinter, full_redraw, print_screen_perfect

__all__ = ["Controller"]

_RAW_MAX = 8000


class _Services:
    """命令可用的能力实现：同时满足 UIService 与 ShellService 协议。

    命令通过 ``ctx.ui`` / ``ctx.io`` 直接调用这些方法完成自身行为；
    这里只是把 controller 已有的能力暴露出去，不额外引入"效应解释"层。
    """

    def __init__(self, controller: "Controller") -> None:
        self._c = controller

    # ---- UIService ----

    def clear_screen(self) -> None:
        self._c._ui_clear_screen()

    def clear_screen_line(self) -> None:
        self._c._ui_clear_line()

    def show_screen(self, *, raw: bool = False) -> None:
        self._c._ui_show_screen(raw=raw)

    async def show_message(self, text: str) -> None:
        await self._c._show_message(text)

    def restore_context(self) -> None:
        self._c._print_context()

    # ---- ShellService ----

    def write(self, data: bytes, *, enter: bool = False) -> None:
        self._c._shell_write(data, enter=enter)

    async def read_line(
        self,
        prompt: str,
        *,
        history_id: str = "cmd",
        cancel: str | None = "",
    ) -> str | None:
        return await self._c._read_line(
            prompt=prompt, include_last=False, cancel=cancel, history_id=history_id
        )


class Controller:
    def __init__(
        self,
        backend: TerminalBackend,
        argv: list[str],
        screen: ScreenModel,
        settings: Any,
        session: Session,
        paths: Any,
    ) -> None:
        self._backend = backend
        self._argv = argv
        self._screen = screen
        self._settings = settings
        self._session = session
        self._paths = paths

        self._pty: Any = None
        self._pty_fd: int = -1
        self._console: Console | None = None
        self._printer: LinePrinter | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stdin_q: asyncio.Queue[bytes] = asyncio.Queue()
        self._key_parser = KeyParser()
        self._key_buf: list[Key] = []
        self._raw = bytearray()
        self._mode = "char"
        self._exit_event = asyncio.Event()
        self._editors: dict[str, LineEditor] = {}
        # 覆盖模式（line）期间子进程是否有输出 —— 有输出即表示屏幕模型已领先于
        # 物理终端（因为 line 模式不透传），退出时必须整屏重绘而非单行恢复。
        self._model_changed_in_overlay = False
        self._services = _Services(self)

    # ---- 主入口 ----

    async def run(self) -> int:
        winsize = Winsize(rows=self._screen.rows, columns=self._screen.columns)
        self._pty = self._backend.spawn(self._argv, winsize)
        self._pty_fd = self._pty.fileno()
        self._console = self._backend.open_console()
        self._printer = LinePrinter(self._console)

        self._loop = asyncio.get_running_loop()
        self._align_model_to_cursor()
        self._loop.add_reader(self._pty_fd, self._on_master_readable)
        self._loop.add_reader(sys.stdin.fileno(), self._on_stdin_readable)
        try:
            self._loop.add_signal_handler(signal.SIGWINCH, self._on_winch)
            self._loop.add_signal_handler(signal.SIGCHLD, self._on_sigchld)
        except (NotImplementedError, RuntimeError):
            pass

        code = 0
        try:
            while not self._exit_event.is_set():
                if self._mode == "char":
                    await self._char_step()
                elif self._mode == "line":
                    await self._line_step()
                elif self._mode == "prompt":
                    await self._prompt_step()
                else:
                    self._mode = "char"
        except _QuitException as e:
            code = e.code
        finally:
            await self._cleanup()
        return code

    def _align_model_to_cursor(self) -> None:
        """把屏幕模型的起始行对齐到宿主终端当前光标所在行。

        模型的初始状态是一张空屏、光标在 (0,0)，但用户进入 LLS 时光标未必在顶行
        （最常见的情况：上一个 shell 的提示符就停在屏幕最后一行）。若不对齐，模型
        认为内容从屏幕顶部开始，而物理上从中间/底部开始，两者差了一段行距；一旦
        需要整屏重绘（按模型行号定位光标），内容与光标就会整体跳到屏幕顶部。

        做法：用 DSR 查询光标行 R（1-based），给模型喂 R-1 个换行，
        使模型光标与物理光标落在同一行。模型顶部因此留出的空行代表
        「模型未建模的历史画面」，重绘时只从第一个非空行开始画，不会把它们搬上屏幕。

        终端不响应 DSR（重定向、非交互、部分嵌入式终端）时保持原样，
        行为与之前一致，不引入回归。
        """
        if self._console is None:
            return
        query = getattr(self._console, "cursor_position", None)
        if query is None:
            return
        position = query()
        if position is None:
            return
        take_pending = getattr(self._console, "take_pending", None)
        if take_pending is not None:
            pending = take_pending()
            if pending:
                # 查询期间用户可能已经敲了键，交给主循环，不丢失
                self._stdin_q.put_nowait(pending)
        row, _column = position
        advance = row - 1
        if advance > 0:
            self._screen.feed(b"\n" * advance)

    async def _cleanup(self) -> None:
        if self._loop is not None:
            for fd in (self._pty_fd, sys.stdin.fileno()):
                if fd >= 0:
                    try:
                        self._loop.remove_reader(fd)
                    except Exception:
                        pass
        if self._pty is not None:
            self._pty.close()
        self._backend.close_console()
        self._save_history()

    # ---- fd 回调 ----

    def _on_master_readable(self) -> None:
        if self._exit_event.is_set():
            return
        try:
            data = os.read(self._pty_fd, 10240)
        except OSError as e:
            if e.errno == errno.EIO:
                self._on_child_exit()
            return
        if not data:
            self._on_child_exit()
            return
        self._record_raw(data)
        self._screen.feed(data)
        if self._mode != "line" and self._console is not None:
            # char / prompt 模式透传（legacy read_stdout: mode != 'line'）
            self._console.write(data)
        elif self._mode == "line":
            # line 模式不透传：模型已领先物理终端，标记脱节，退出时需整屏重绘。
            self._model_changed_in_overlay = True

    def _on_stdin_readable(self) -> None:
        if self._exit_event.is_set():
            return
        try:
            data = os.read(sys.stdin.fileno(), 10240)
        except (OSError, BlockingIOError):
            return
        if data:
            self._stdin_q.put_nowait(data)

    def _on_winch(self) -> None:
        try:
            size = os.get_terminal_size()
        except OSError:
            return
        self._screen.resize(size.lines, size.columns)
        if self._pty is not None:
            self._pty.resize(size.lines, size.columns)

    def _on_sigchld(self) -> None:
        self._reap()
        self._on_child_exit()

    def _on_child_exit(self) -> None:
        """子进程退出的唯一入口：终止忙轮询并唤醒主循环。幂等。"""
        if self._exit_event.is_set():
            return
        if self._loop is not None and self._pty_fd >= 0:
            try:
                self._loop.remove_reader(self._pty_fd)
            except Exception:
                pass
        self._exit_event.set()
        self._reap()

    def _reap(self) -> None:
        if self._pty is None:
            return
        try:
            os.waitpid(self._pty.pid, os.WNOHANG)
        except (ChildProcessError, OSError):
            pass

    def _record_raw(self, data: bytes) -> None:
        self._raw.extend(data)
        if len(self._raw) > _RAW_MAX:
            overflow = len(self._raw) - _RAW_MAX
            del self._raw[:overflow]
            # legacy 的 _raw 是 str（按字符截断，天然不劈开多字节字符）；此处是
            # bytes，需对齐 UTF-8 边界，避免截断时把多字节字符劈成半个导致乱码。
            while self._raw and (self._raw[0] & 0xC0) == 0x80:
                del self._raw[0]

    # ---- Shell 能力 ----

    def _pty_write(self, data: bytes) -> None:
        if self._exit_event.is_set() or self._pty is None:
            return
        try:
            self._pty.write(data)
        except OSError as e:
            if e.errno == errno.EIO:
                self._on_child_exit()

    def _shell_write(self, data: bytes, *, enter: bool = False) -> None:
        """写子进程；``enter`` 为 True 时追加换行（若末尾尚无换行）。"""
        if enter and not data.endswith(b"\n"):
            data += b"\n"
        self._pty_write(data)

    # ---- UI 能力 ----

    def _ui_show_screen(self, raw: bool = False) -> None:
        """清屏并显示当前屏幕（legacy cmd_show / cmd_raw）。"""
        if self._console is None:
            return
        self._console.write(b"\x1b[2J\x1b[H\r")
        if raw:
            # legacy cmd_raw: print(screen.raw())
            self._console.write(bytes(self._raw))
            self._console.write(b"\r\n")
        else:
            print_screen_perfect(self._screen, self._console, end="\r\n")

    def _ui_clear_screen(self) -> None:
        """清屏（legacy cmd_clear）。"""
        if self._console is not None:
            self._console.write(b"\x1b[2J\x1b[H\r")

    def _ui_clear_line(self) -> None:
        """清除当前行（legacy ``print('\\033[2K\\r')``）。"""
        assert self._printer is not None
        self._printer.clear_lines(1, 1)

    def _print_context(self) -> None:
        """恢复被覆盖模式占用的画面（对应 legacy common.print_context）。

        判据的本质 —— **模型与物理终端是否已经脱节**：

        * line 模式不透传子进程输出（见 ``_on_master_readable``），所以期间子进程
          只要有输出，模型就领先于物理终端，必须整屏重绘才能同步；
        * 反之若子进程全程无输出（用户只是敲了 conf/show 这类命令），物理终端仍是
          进入覆盖模式前的样子，只需抹掉 ``read_line`` 覆盖的那一行即可 ——
          开销量级相差两个数量级，且不产生整屏闪烁。

        早年用 ``alternate``（备用缓冲区）当判据是「以症状代替病因」：
        vim 在备用缓冲区几乎必然重绘，因而几乎必然脱节，但二者并不等价 ——
        vim 静止时不需要重绘（白闪），而主缓冲区下持续输出时却同样会脱节（漏画）。
        故此处使用显式的脱节标记 ``_model_changed_in_overlay``。
        """
        if self._console is None:
            return
        try:
            if self._model_changed_in_overlay:
                full_redraw(self._screen, self._console)
            else:
                # 未脱节：物理终端上只有光标所在行被 read_line 覆盖过，抹掉重写即可。
                row = self._screen.cursor.y
                self._console.write(b"\x1b[2K\r" + self._screen.row_text(row).encode())
        finally:
            self._model_changed_in_overlay = False

    async def _show_message(self, text: str) -> None:
        """show_line：显示一行消息，等任意键，清除。不改变模式。

        精确还原 legacy show_line = read_line(msg, max_chars=1, backspace='b')：
        读任意一个键即返回（不等回车），清除显示，绝不改变 mode。
        """
        assert self._printer is not None
        # print_lines 返回的 lines_all 是多行文本的真实行数（如 ls 的 3 行），
        # clear_lines 必须按真实行数撤回，否则只清最后一行、光标回不到第一行。
        lines_all, lines_cur = self._printer.print_lines(
            text, None, self._screen.columns
        )
        await self._next_key()
        self._printer.clear_lines(lines_all, lines_cur)

    # ---- 模式 ----

    async def _char_step(self) -> None:
        """char 模式：透传输入，拦截 Ctrl-E / Ctrl-G。"""
        data = await self._wait_stdin()
        if data is None:
            return
        for i, b in enumerate(data):
            if b == 0x05:  # Ctrl-E → line
                if i:
                    self._pty_write(data[:i])
                self._model_changed_in_overlay = False
                self._mode = "line"
                return
            if b == 0x07:  # Ctrl-G → prompt
                if i:
                    self._pty_write(data[:i])
                self._model_changed_in_overlay = False
                self._mode = "prompt"
                return
        self._pty_write(data)

    async def _line_step(self) -> None:
        """命令模式：持续读命令并执行，直到模式被切换 / 退出 / 子进程退出。

        命令自己完成全部行为（清屏、显示、读输入、写子进程），返回值只承载
        「改模式」和「退出」这两件 controller 才能做的事。
        """
        while self._mode == "line" and not self._exit_event.is_set():
            # legacy line_mode 用 read_line(cancel='q', include_last=False,
            # id='line_mode', no_save=['q'])；提示符是 read_line 默认 ':'；
            # Ctrl-C 返回 'q'（cancel 值），进而走 cmd_quit 退出。
            line = await self._read_line(
                prompt=":", include_last=False, cancel="q", history_id="line_mode"
            )
            if line is None:  # 子进程退出
                self._mode = "char"
                break
            if line == "":
                continue
            cmd, args = _split_command(line)
            ctx = self._make_command_context()
            try:
                result = await execute_command(cmd, ctx, args)
            except Exception as e:  # noqa: BLE001
                await self._show_message(f"error: {e}")
                continue
            if result is None:
                await self._show_message(f"unknown command: {cmd}")
                continue
            if result.quit:
                raise _QuitException(0)
            if result.mode is not None and result.mode != self._mode:
                self._mode = result.mode
                if result.mode == "char":
                    # 退出 line 回 char：恢复被 read_line 覆盖的画面（legacy print_context）
                    self._print_context()

    async def _prompt_step(self) -> None:
        """generate：精确还原 legacy cmd_generate + cmd_generate_wrap。

        结束（含 instruct 为空、异常等所有退出路径）统一在 finally 里
        print_context 恢复终端 + 回到 char（精确还原 legacy prompt_mode 的 finally）。
        """
        try:
            await self._prompt_step_body()
        finally:
            self._print_context()
            self._mode = "char"

    async def _prompt_step_body(self) -> None:
        prompt = "gen"
        default = "u"

        # 1. 读指令
        instruct = await self._read_instruct(prompt)
        if instruct is None or instruct == "":
            return

        context = self._make_command_context().make_context()
        role = self._settings.role()
        cmd, think = "", ""

        # 2. 生成（# 预设命令则跳过 AI）
        if "#" in instruct:
            parts = instruct.split("#")
            instruct = parts[0].strip()
            cmd = parts[-1].strip()
            output: Any = None
        else:
            output = self._gen_messages(instruct, context, role)

        confirm_info = ", confirm?"
        flags = "[y/u/i/n/e/s/r/k/t]"
        show_think = False
        gen_time = time.time()
        enter = True

        while True:
            if output is not None:
                # 流式生成：精确还原 legacy cmd_generate 的动态多行撤回。
                # 初始直接写 "waiting..."（legacy: os.write('\033[2K\r...')，不先 clear）。
                lines_all, lines_cur = 1, 1
                assert self._printer is not None
                self._printer.print_lines(
                    "(gen-cmd): waiting...", None, self._screen.columns
                )
                cancelled = False
                gen_cmd, gen_think = "", ""
                prev_len = 0
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
                        # 动态跟踪 lines_all/lines_cur：AI 命令折多行时按真实行数撤回
                        lines_all, lines_cur = await self._print(
                            text, prev_len, lines_all, lines_cur
                        )
                        prev_len = len(text)
                except asyncio.CancelledError:
                    cancelled = True
                # 结束后撤回流式帧（legacy: clear_lines(lines_all, lines_cur)）
                self._printer.clear_lines(lines_all, lines_cur)
                if not cancelled:
                    cmd, think = gen_cmd, gen_think
                else:
                    if gen_cmd == "" and gen_think != "":
                        cmd, think = gen_think, ""
                    else:
                        cmd, think = gen_cmd, gen_think
                gen_time = time.time()
                output = None

            if cmd:
                self._record_line_history(cmd, "cmd")

            flags_text = flags.replace(default, default.upper())
            text = f"(gen-cmd): {cmd}{confirm_info} {flags_text} "
            if show_think:
                text = f"(gen-think): {think}\n{text}"
            show_think = False
            confirm_info = ", confirm?"

            confirm = await self._read_line(
                prompt=text, include_last=False, cancel="cancel", exit="n"
            )
            confirm = (confirm or "").lower()
            if confirm == "":
                confirm = default

            if confirm in ("y", "yes"):
                break
            elif confirm in ("u", "use"):
                break
            elif confirm in ("i", "input"):
                enter = False
                break
            elif confirm in ("n", "no", "q", "quit", "exit"):
                cmd = ""
                break
            elif confirm in ("k", "think"):
                show_think = True
            elif confirm in ("r", "re", "retry"):
                output = self._gen_messages(instruct, context, role)
            elif confirm in ("e", "edit"):
                instruct = await self._read_instruct(prompt, value=instruct)
                if instruct is None or instruct == "":
                    cmd = ""
                    break
                output = self._gen_messages(instruct, context, role)
            elif confirm in ("t", "teach"):
                default = "y"
                cmd = await self._read_line(
                    prompt="(gen-cmd): ", include_last=False, history_id="cmd"
                )
                if cmd == "" or cmd is None:
                    break
            elif confirm in ("s", "show", "status"):
                self._ui_show_screen()
            elif confirm == "cancel":
                if time.time() - gen_time > 0.6:
                    cmd = ""
                    break
                gen_time = time.time()
            else:
                confirm_info = ", please input 'y' or 'n':"

        # 3. 结果处理（cmd_generate_wrap 语义）
        if cmd:
            self._session.append(instruct, context, cmd)
            data = cmd.encode() + (b"\n" if enter else b"")
            self._pty_write(data)
            await asyncio.sleep(0.1)
            self._ui_show_screen()

    # ---- 生成辅助 ----

    def _gen_messages(self, instruct: str, context: Any, role: Any) -> Any:
        messages = self._session.replay(role, instruct, context)
        return stream_chat(self._settings.ai, messages)

    async def _read_instruct(self, prompt: str, value: str = "") -> str | None:
        """读指令，支持 / 命令切换（legacy read_instruct）。"""
        instruct = None
        while instruct is None:
            instruct = await self._read_line(
                prompt=f"({prompt}-instruct): ",
                include_last=False,
                value=value,
                history_id="instruct",
            )
            value = instruct or ""
            instruct = (instruct or "").strip()
            if instruct[:1] == "/":
                cmd = instruct[1:]
                args = None
                instruct = None
                value = ""
                if " " in cmd and cmd[:1] != " ":
                    idx = cmd.find(" ")
                    args = cmd[idx + 1 :].strip()
                    cmd = cmd[:idx].strip()
                if cmd in ("s", "show", "status"):
                    self._ui_show_screen()
                elif cmd in ("m", "mode"):
                    await self._show_message(f"mode: {args or self._settings.role_id}")
                elif cmd in ("c", "ch", "chat"):
                    instruct = args
                    self._settings.role_id = "chat"
                elif cmd in ("t", "text"):
                    instruct = args
                    self._settings.role_id = "completion"
        return instruct

    # ---- 行读取（精确还原 legacy read_line）----

    async def _read_line(
        self,
        prompt: str = ":",
        include_last: bool = False,
        value: str = "",
        cancel: str | None = None,
        exit: str | None = None,
        history_id: str = "_default",
    ) -> str | None:
        """精确还原 legacy display.read_line 的交互与返回值语义。

        返回：输入内容；Ctrl-C → cancel 值（或 None）；Ctrl-D → exit 值。
        """
        editor = self._editors.get(history_id)
        if editor is None:
            editor = LineEditor()
            self._editors[history_id] = editor
        editor.reset(value)

        assert self._printer is not None
        width = self._screen.columns
        lines_all, lines_cur = self._printer.print_lines(
            prompt + editor.text, len(prompt) + editor.cursor, width
        )
        while True:
            key = await self._next_key()
            if key is None:
                return None
            if key.control == "ctrl_c":
                if cancel is not None:
                    return cancel
                line = editor.text
                if include_last:
                    line += "\x03"
                return line
            if key.control == "ctrl_d":
                if exit is not None or cancel is not None:
                    return exit if exit is not None else cancel
                line = editor.text
                if include_last:
                    line += "\x04"
                return line
            if key.control == "enter":
                self._printer.clear_lines(lines_all, lines_cur)
                return editor.submit()
            if key.control == "backspace":
                editor.backspace()
            elif key.control == "delete":
                editor.delete()
            elif key.control == "left":
                editor.left()
            elif key.control == "right":
                editor.right()
            elif key.control == "home":
                editor.home()
            elif key.control == "end":
                editor.end()
            elif key.control == "up":
                editor.history_up()
            elif key.control == "down":
                editor.history_down()
            elif key.control in ("escape", "tab", "unknown"):
                pass
            elif key.char:
                editor.insert(key.char)
            self._printer.clear_lines(lines_all, lines_cur)
            lines_all, lines_cur = self._printer.print_lines(
                prompt + editor.text, len(prompt) + editor.cursor, width
            )

    async def _print(
        self,
        text: str,
        prev_len: int | None,
        lines_all: int = 1,
        lines_cur: int = 1,
    ) -> tuple[int, int]:
        """流式刷新：精确还原 legacy cmd_generate 的 clear_lines + print_lines。

        legacy 用动态 ``lines_all``/``lines_cur`` 撤回上一帧（AI 命令折成多行时
        ``lines_all`` > 1，clear_lines 必须按真实行数撤回，否则残留）。
        本函数返回新的 ``(lines_all, lines_cur)``，供调用处持续跟踪。
        """
        assert self._printer is not None
        if prev_len is not None and len(text) < prev_len:
            self._printer.clear_lines(lines_all, lines_cur, clear=True)
        else:
            self._printer.clear_lines(lines_all, lines_cur, clear=False)
        return self._printer.print_lines(text, None, self._screen.columns)

    def _record_line_history(self, value: str, history_id: str) -> None:
        editor = self._editors.get(history_id)
        if editor is None:
            editor = LineEditor()
            self._editors[history_id] = editor
        editor.record(value)

    # ---- 键读取 ----

    async def _next_key(self) -> Key | None:
        """取一个键；子进程退出时返回 None。不丢键。"""
        while True:
            if self._key_buf:
                return self._key_buf.pop(0)
            if self._exit_event.is_set():
                return None
            data = await self._wait_stdin()
            if data is None:
                return None
            self._key_buf.extend(self._key_parser.feed(data))
            if self._key_buf:
                return self._key_buf.pop(0)

    async def _wait_stdin(self) -> bytes | None:
        """等一段 stdin 输入；若子进程先退出则返回 None。"""
        get_task = asyncio.ensure_future(self._stdin_q.get())
        exit_task = asyncio.ensure_future(self._exit_event.wait())
        done, pending = await asyncio.wait(
            {get_task, exit_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for t in pending:
            t.cancel()
        if exit_task in done or get_task not in done:
            return None
        return get_task.result()

    # ---- 辅助 ----

    def _make_command_context(self) -> CommandContext:
        return CommandContext(
            ui=self._services,
            io=self._services,
            screen=self._screen,
            ai_config=self._settings.ai,
            roles=self._settings.roles,
            role_id=self._settings.role_id,
            session=self._session,
            raw=bytes(self._raw),
            cwd=None,
            shell=os.environ.get("SHELL"),
            last_command=None,
        )

    def _save_history(self) -> None:
        if not self._session.turns:
            return
        try:
            self._paths.config_dir.mkdir(parents=True, exist_ok=True)
            with open(self._paths.history_file, "a", encoding="utf-8") as f:
                f.write(self._session.to_jsonl() + "\n")
        except OSError:
            pass


class _QuitException(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code = code


def _split_command(line: str) -> tuple[str, str | None]:
    line = line.strip()
    if " " in line:
        cmd, _, args = line.partition(" ")
        return cmd.strip(), args.strip() or None
    return line, None
