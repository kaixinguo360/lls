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
from lls.ui import (
    LinePrinter,
    overlay_anchor,
    print_screen_perfect,
    render_row_cells,
    wrap_multi_lines,
)

__all__ = ["Controller"]

_RAW_MAX = 8000

# 收到 ESC 后等待后续字节的时间。方向键等序列几乎立刻到达，
# 超过这个时间仍未收全就判定为独立 Esc 键（readline / tmux 的做法）。
ESC_PENDING_TIMEOUT = 0.05

# 子进程输出后，等这么久没有新输出才重绘覆盖层。
# 目的是让程序（尤其全屏程序）的整屏重绘完整落地，避免覆盖层插在中间。
OVERLAY_REDRAW_DELAY = 0.05

# _read_line 的哨兵返回值：用户按下 Esc（用于退出 line 模式等场景）。
ESCAPE = object()


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
        # 当前正在显示的提示符状态：(prompt, 文本, 光标, lines_all, lines_cur)。
        # line 模式下若子进程有输出，据此把提示符临时移开、写输出、再画回来，
        # 使输出能够实时可见（类似 tmux 里命令行固定在底部、上方内容持续更新）。
        self._active_prompt: tuple[str, str, int, int, int] | None = None
        # 覆盖层几何：位置由**模型实时光标**推导，这里只记录当前帧的实际占用。
        #   _overlay_anchor_row  —— 当前帧的起始行；None 表示当前没有覆盖层
        #   _overlay_lines       —— 当前帧占用的行数
        self._overlay_anchor_row: int | None = None
        self._overlay_lines = 0
        # 覆盖层的延迟重绘定时器。
        # 子进程（如 vim）重绘整屏时会连续发出多块输出；若在每块之间插入
        # 覆盖层的绘制，就会打断它的绘制序列（实测第 1 行内容被 ':' 覆盖）。
        # 因此输出期间只擦除、不重绘，等输出静默后再画一次。
        self._overlay_timer: asyncio.TimerHandle | None = None
        # 消息展示期间暂停透传：否则后台输出会把消息冲乱。
        # 期间收到的输出仍进屏幕模型，展示结束后按需重绘补齐。
        self._suppress_passthrough = False
        self._output_during_message = False
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
                try:
                    if self._mode == "char":
                        await self._char_step()
                    elif self._mode == "line":
                        await self._line_step()
                    elif self._mode == "prompt":
                        await self._prompt_step()
                    else:
                        self._mode = "char"
                except _QuitException:
                    raise
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001
                    # 任何未捕获异常都不能让 traceback 直接写到 stderr:
                    # 那些字节不经过显示逻辑，会撑滚终端并冲掉覆盖层。
                    # 统一以覆盖层提示，然后回到可用的状态。
                    try:
                        await self._show_message(f"error: {e}")
                    except Exception:  # noqa: BLE001
                        pass
                    self._mode = "char"
        except _QuitException as e:
            code = e.code
        finally:
            await self._cleanup()
        return code

    def _align_model_to_cursor(self) -> None:
        """把模型光标对齐到宿主终端当前光标所在行（用 CUP，不用换行）。

        模型的初始状态是一张空屏、光标在 (0,0)，但用户进入 LLS 时光标未必在顶行
        （最常见：上一个 shell 的提示符就停在屏幕底部）。若不对齐，模型认为内容
        从屏幕顶部开始，而物理上从中间/底部开始，两者差了一段行距。

        **必须用 CUP 定位，不能喂换行**：喂换行会让模型把这些行标成 dirty
        （"内容变了"），随后按模型重绘时就会用空白覆盖它们 —— 而物理上那几行
        是 LLS 启动前留下的真实内容，模型根本没收到过，属于**未知区域**，
        绝不能碰（实测会把 PRE1..PRE5 之类的历史输出擦成空白）。

        CUP 只移动光标，不标记任何行，未知区域因此保持原样。
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
        row, column = position
        if row > 1 or column > 1:
            self._screen.feed(f"\x1b[{row};{column}H".encode())
            # 对齐用的定位不该被当成"画面变化"
            self._screen.clear_dirty()

    async def _cleanup(self) -> None:
        if self._overlay_timer is not None:
            self._overlay_timer.cancel()
            self._overlay_timer = None
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
        if self._console is None:
            return
        if self._mode != "line":
            # char / prompt 模式直接透传（legacy read_stdout: mode != "line"）：
            # 这条路径上覆盖层不存在，物理终端由子进程独占，透传是正确且最快的。
            self._console.write(data)
        else:
            # line 模式**绝不透传子进程字节**：物理终端的一切内容都由屏幕模型
            # 投影而来（见 _render_from_model）。透传会让「子进程画的」与
            # 「覆盖层画的」争用同一个终端，只能靠擦除/还原/时序补丁勉强维持。
            if self._suppress_passthrough:
                # 消息展示期间不重绘，避免冲乱消息；内容已进模型，稍后补齐。
                self._output_during_message = True
            else:
                self._render_from_model()

    def _render_from_model(
        self, *, keep_overlay: bool = True, leave_cursor: bool = True
    ) -> None:
        """唯一的渲染路径：把模型内容投影到物理终端。

        不写子进程的原始字节，只写「模型算出来的画面」：

        1. 覆盖层占用的行并入 dirty —— 它们现在既要腾出来，又要按模型显示，
           所以「让出覆盖层」和「重绘内容」是同一次写，不是两次；
        2. 重绘所有 dirty 行（pyte 精确标记变化的行）；
        3. 把物理光标放到模型光标处。

        这样物理终端恒等于模型，覆盖层是唯一浮在上面的东西。

        ``keep_overlay=False``：只让出画面，不重新调度覆盖层绘制
        —— 用于退出覆盖模式、或即将在新的位置重画覆盖层时。
        ``leave_cursor=False``：不动光标，调用方紧接着会自行定位。
        """
        if self._console is None:
            return
        dirty = set(self._screen.dirty)
        if self._overlay_anchor_row is not None and self._overlay_lines:
            top = self._overlay_anchor_row
            bottom = min(top + self._overlay_lines, self._screen.rows)
            dirty.update(range(top, bottom))
        self._overlay_anchor_row = None
        self._overlay_lines = 0
        self._screen.clear_dirty()

        if dirty:
            out = []
            for row in sorted(dirty):
                if not 0 <= row < self._screen.rows:
                    continue
                out.append(f"\x1b[{row + 1};1H")
                out.append(render_row_cells(self._screen.row_cells(row)))
                out.append("\x1b[0K")
            if out:
                self._console.write("".join(out).encode())
        if leave_cursor:
            self._place_physical_cursor()
        if keep_overlay:
            self._schedule_overlay_redraw()

    def _schedule_overlay_redraw(self) -> None:
        """输出到达后（重新）安排一次覆盖层重绘，等输出静默才真正执行。"""
        if self._overlay_timer is not None:
            self._overlay_timer.cancel()
            self._overlay_timer = None
        if self._loop is None:
            self._flush_overlay_redraw()
            return
        self._overlay_timer = self._loop.call_later(
            OVERLAY_REDRAW_DELAY, self._flush_overlay_redraw
        )

    def _flush_overlay_redraw(self) -> None:
        """输出静默后重画覆盖层，让它浮在最新画面上。

        基准直接取模型光标 —— 那就是子进程当前所在的位置，覆盖层贴着它显示。
        不再需要另存子进程光标：覆盖层的绘制不写模型，模型光标始终是真的。
        """
        self._overlay_timer = None
        active = self._active_prompt
        if active is None or self._console is None:
            return
        prompt, text, cursor, _lines_all, _lines_cur = active
        _, new_all = self._draw_overlay(prompt + text, cursor)
        self._active_prompt = (prompt, text, cursor, new_all, new_all)

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
        """让出画面（对应 legacy print_context）：收起覆盖层，按模型还原。

        就是 :meth:`_render_from_model` 的「不重画覆盖层」形态 ——
        覆盖层的行并入 dirty 后一次写回，不需要「先擦除再重绘」。
        """
        self._render_from_model(keep_overlay=False)

    async def _show_message(self, text: str) -> None:
        """show_line：显示一行消息，等任意键，清除。不改变模式。

        精确还原 legacy show_line = read_line(msg, max_chars=1, backspace='b')：
        读任意一个键即返回（不等回车），清除显示，绝不改变 mode。

        展示期间暂停透传子进程输出，否则后台输出会把消息冲乱；期间到达的输出
        仍写入屏幕模型，展示结束后若确有输出则重绘一次补齐。

        消息也是覆盖层：它可能有多行（如 ``ls`` 的实例列表），必须按覆盖层几何
        定位（放不下就上移），否则会把终端撑得滚动、顶掉上方内容。
        """
        assert self._printer is not None
        self._draw_overlay(text)
        self._suppress_passthrough = True
        self._output_during_message = False
        try:
            await self._next_key()
        finally:
            self._suppress_passthrough = False
            self._render_from_model(keep_overlay=False)  # 收起消息，按模型还原它占的行
        if self._output_during_message:
            self._output_during_message = False
            self._print_context()

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
                self._mode = "line"
                return
            if b == 0x07:  # Ctrl-G → prompt
                if i:
                    self._pty_write(data[:i])
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
                prompt=":",
                include_last=False,
                cancel="q",
                history_id="line_mode",
                escape=ESCAPE,
            )
            if line is None:  # 子进程退出
                self._mode = "char"
                break
            if line is ESCAPE:
                # Esc：退出 line 模式回到 char（与 Ctrl-E 进入相对），
                # 恢复被提示符覆盖的画面。
                #
                # 同批到达的后续按键（用户按 Esc 后立刻敲的字）还在 _key_buf 里，
                # 退出后它们属于 char 模式（应交给子进程），这里回填到 stdin 队列，
                # 否则会被丢弃。
                self._requeue_pending_keys()
                self._mode = "char"
                self._print_context()
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

        异常必须在这里捕获并以覆盖层显示：否则 Python 会把 traceback 直接写到
        stderr，那些字节不经过任何显示逻辑，会把终端撑得滚动、并冲掉覆盖层，
        物理画面与屏幕模型随即错位。
        """
        try:
            await self._prompt_step_body()
        except Exception as e:  # noqa: BLE001
            await self._show_message(f"error: {e}")
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
                # 流式生成：走覆盖层（绝对定位、按行区间擦除），不滚动终端。
                lines_all, lines_cur = 1, 1
                self._draw_overlay("(gen-cmd): waiting...")
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
                        lines_all, lines_cur = await self._print(
                            text, prev_len, lines_all, lines_cur
                        )
                        prev_len = len(text)
                except asyncio.CancelledError:
                    cancelled = True
                # 撤掉流式帧，并把它占的行按模型还原
                self._render_from_model(keep_overlay=False)
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

    def _draw_overlay(self, text: str, cursor: int | None = None) -> tuple[int, int]:
        """绘制覆盖层内容（命令行 / 消息 / 流式生成），自约束位置。

        覆盖层（命令行、消息、生成中的命令预览）都浮在画面上，是临时显示。
        它若不自己约束位置，画到屏幕底之外就会让终端滚动、把上方内容顶走，
        物理画面与屏幕模型随之错位 —— 之后子进程的回显会被写进错误的行/列。

        因此这里由覆盖层自己负责：

        1. 算出需要几行、上一帧占了哪几行，按**行区间**精确擦除（位置可能变）；
        2. 用 :func:`overlay_anchor` 定出起始行：靠近基准行，放不下就整体上移；
        3. 以绝对定位绘制。

        **不写模型**：覆盖层是本程序的浮动层，不是子进程的画面。
        模型只记录子进程，光标始终是子进程的真实位置。

        **位置只取模型的实时光标**，不记住「进入时的位置」：
        覆盖层是浮动的，就该贴着子进程当前所在的行；子进程（vim、shell 提示符）
        随时可能移动光标，记住旧位置只会让覆盖层漂在无关的地方。

        ``cursor`` 为 None 时表示纯展示（消息、流式预览），光标停在文末。
        返回 ``(anchor, lines_all)``。
        """
        assert self._printer is not None
        if self._overlay_timer is not None:
            # 即将重绘，取消挂起的延迟重绘，避免同一帧画两次
            self._overlay_timer.cancel()
            self._overlay_timer = None
        width = self._screen.columns
        _, lines_needed = wrap_multi_lines(text, width)
        anchor = overlay_anchor(
            self._screen.cursor.y, lines_needed, self._screen.rows
        )
        open('/tmp/m.log','a').write('DRAW anchor=%d model=(%d,%d) prev=%r\n' % (anchor, self._screen.cursor.y, self._screen.cursor.x, self._overlay_anchor_row))
        # 「让出旧位置」只在真的换位置时才做：
        # 若新锚点与旧锚点完全相同，先恢复模型内容再立刻被覆盖是纯粹的重复写
        # （实测每敲一个字符就白写一遍整行）。位置没变时直接覆盖即可。
        if self._overlay_anchor_row != anchor:
            # 让出旧位置（并入 dirty 一次写回），不重画覆盖层、不动光标
            # —— 紧接着 print_lines 会画到新锚点
            self._render_from_model(keep_overlay=False, leave_cursor=False)
        lines_all, lines_cur = self._printer.print_lines(
            text, cursor, width, anchor_row=anchor
        )
        self._overlay_anchor_row = anchor
        self._overlay_lines = lines_all
        return anchor, lines_all

    def _place_physical_cursor(self) -> None:
        """把物理光标放到模型光标处。

        覆盖层绘制会移动物理光标；子进程回显必须落在模型光标的位置，
        否则内容会被写到错误的行列。这里显式对齐一次。
        """
        if self._console is None:
            return
        c = self._screen.cursor
        self._console.write(f"\x1b[{c.y + 1};{c.x + 1}H".encode())

    async def _read_line(
        self,
        prompt: str = ":",
        include_last: bool = False,
        value: str = "",
        cancel: str | None = None,
        exit: str | None = None,
        history_id: str = "_default",
        escape: Any = None,
    ) -> Any:
        """精确还原 legacy display.read_line 的交互与返回值语义。

        返回：输入内容；Ctrl-C → cancel 值（或 None）；Ctrl-D → exit 值；
        按下 Esc 且 ``escape`` 非 None 时返回 ``escape`` 值。
        """
        editor = self._editors.get(history_id)
        if editor is None:
            editor = LineEditor()
            self._editors[history_id] = editor
        editor.reset(value)

        assert self._printer is not None
        width = self._screen.columns
        anchor, lines_all = self._draw_overlay(
            prompt + editor.text, len(prompt) + editor.cursor
        )
        lines_cur = lines_all
        # **立刻**登记活跃覆盖层，而不是等到第一次按键之后。
        # 否则「刚进入 line、用户在等、子进程此时输出」的窗口里
        # _active_prompt 仍是 None，输出会直接透传并把覆盖层冲掉
        # （实测 exec 启动 vim 时 ':' 就是这样消失、之后不再出现）。
        self._active_prompt = (
            prompt,
            editor.text,
            len(prompt) + editor.cursor,
            lines_all,
            lines_cur,
        )
        try:
            while True:
                key = await self._next_key()
                if key is None:
                    return None
                if key.control == "terminal_response":
                    # 终端对查询（DSR/DA）的应答，不是用户输入：
                    # 转发给子进程 —— 它在等这个结果；绝不当按键编辑或显示。
                    self._pty_write(key.raw)
                    continue
                if key.control in ("ctrl_c", "ctrl_d", "enter") or (
                    key.control == "escape" and escape is not None
                ):
                    # 离开输入循环：收起覆盖层，把它占的行按模型还原
                    # （它可能占多行，且位置与进入时不同）
                    self._render_from_model(keep_overlay=False)
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
                        return editor.submit()
                    return escape  # Esc
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
                # 内容变化后重新布局：行数可能变，位置随之上下调整。
                anchor, lines_all = self._draw_overlay(
                    prompt + editor.text, len(prompt) + editor.cursor
                )
                lines_cur = lines_all
                self._active_prompt = (
                    prompt,
                    editor.text,
                    len(prompt) + editor.cursor,
                    lines_all,
                    lines_cur,
                )
        finally:
            # 覆盖层已不在显示，之后的输出不再需要绕过它。
            self._active_prompt = None

    async def _print(
        self,
        text: str,
        prev_len: int | None,
        lines_all: int = 1,
        lines_cur: int = 1,
    ) -> tuple[int, int]:
        """流式刷新生成中的命令预览（覆盖层）。

        生成过程每收到一段内容就重画一次，内容可能折成多行并不断变化。
        走覆盖层几何（按行区间擦除 + 绝对定位重画），因此不会把终端撑得滚动。
        返回新的 ``(lines_all, lines_cur)``，供调用处持续跟踪。
        """
        anchor, new_all = self._draw_overlay(text)
        return new_all, new_all

    def _record_line_history(self, value: str, history_id: str) -> None:
        editor = self._editors.get(history_id)
        if editor is None:
            editor = LineEditor()
            self._editors[history_id] = editor
        editor.record(value)

    def _requeue_pending_keys(self) -> None:
        """把尚在键缓冲里的按键回填到 stdin 队列。

        用于退出覆盖模式时：用户可能在同一个字节块里先按了 Esc（退出）又敲了
        别的字；这些字节属于新的模式（char 模式下应交给子进程），
        直接丢弃会表现为「按键失灵」。
        """
        if not self._key_buf:
            return
        data = b"".join(k.raw for k in self._key_buf)
        self._key_buf.clear()
        if data:
            self._stdin_q.put_nowait(data)

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
            if not self._key_buf and self._key_parser.pending_escape:
                # 收到 ESC 但还没决定它是独立按键还是序列前缀：
                # 等一个短超时，看后续字节是否到来（方向键的 "[A" 等几乎立刻到达）。
                more = await self._wait_stdin(ESC_PENDING_TIMEOUT)
                if more:
                    self._key_buf.extend(self._key_parser.feed(more))
                # 仍未收全则判定为独立 Esc（readline / tmux 的做法）
                if not self._key_buf and self._key_parser.pending_escape:
                    flushed = self._key_parser.flush_escape()
                    if flushed is not None:
                        self._key_buf.append(flushed)
            if self._key_buf:
                return self._key_buf.pop(0)

    async def _wait_stdin(self, timeout: float | None = None) -> bytes | None:
        """等一段 stdin 输入；若子进程先退出则返回 None。

        ``timeout`` 到期仍无输入时返回 ``None``（与子进程退出同一返回值，
        调用方需通过 ``self._exit_event`` 区分）。
        """
        get_task = asyncio.ensure_future(self._stdin_q.get())
        exit_task = asyncio.ensure_future(self._exit_event.wait())
        tasks: set[asyncio.Future[Any]] = {get_task, exit_task}
        done, pending = await asyncio.wait(
            tasks,
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
        if get_task in done:
            return get_task.result()
        return None

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
