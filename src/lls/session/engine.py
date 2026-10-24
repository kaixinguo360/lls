"""Engine —— 终端引擎：PTY、屏幕模型、渲染、输入、信号。

**本模块不认识任何具体业务**：不知道什么是命令。它只提供原语，
由外部注册的处理器（:class:`~lls.session.services.Handler`）决定如何响应输入。

主循环做的唯一一件事是反复调用当前处理器的 ``step()``。处理器想要切换，
通过 :meth:`Services.set_handler` 请求一个名字；引擎按注册表查找并切换，
名字的含义完全由应用层决定。

这样任何行为都能实现：默认透传、命令分发、定时监控、全文界面……都是
处理器与命令的组合，无需改动引擎。
"""

from __future__ import annotations

import asyncio
import errno
import os
import select
import signal
import sys
import time
from typing import Any

from lls.edit import LineEditor
from lls.term import ScreenModel
from lls.tty import Console, Key, KeyParser, PtySession, TerminalBackend, Winsize
from lls.ui import (
    LinePrinter,
    overlay_anchor,
    render_row_cells,
    wrap_multi_lines,
)

from .services import Handler, Services

__all__ = ["Engine"]

# 收到 ESC 后等待后续字节的时间。方向键等序列几乎立刻到达，
# 超过这个时间仍未收全就判定为独立 Esc 键（readline / tmux 的做法）。
ESC_PENDING_TIMEOUT = 0.05

# 子进程输出后，等这么久没有新输出才重绘覆盖层。
# 目的是让程序（尤其全屏程序）的整屏重绘完整落地，避免覆盖层插在中间。
OVERLAY_REDRAW_DELAY = 0.05

# char 模式下模型追赶子进程的调度参数。
#
# 解析是纯 Python，一整屏重绘约 10ms。一次 read 可能拿到 10KB（十几帧），
# 同步解析完再返回会让事件循环被独占上百毫秒，期间读不到新输出，显示只能
# 一顿一顿地刷新。分批后每轮只解析一小片，读写都及时。
#
# 切片与预算的取值来自「30 帧整屏重绘下的帧间隔」测量：不分批约 200ms，
# 2048 字节配 4ms 预算约 120ms；切片缩到 512 字节反而更差（约 170ms），
# 因为追赶轮次增多，每轮固定开销占了上风。
_FEED_SLICE = 2048           # 单次喂给模型的最大字节数
_FEED_BUDGET = 0.004         # 单轮解析的时间预算（秒）
_BACKLOG_LIMIT = 256 * 1024  # 积压上限：超过就一次喂完，避免无限增长

# 每次 master 可读时，一口气读走全部可用数据（合并成一笔写出）。
#
# 分笔写会让全屏程序的中间态上屏：vim 启动的一帧含「切备用屏 → 清屏 → 逐行
# 绘制」，分三次写就会闪三次。上限防止连续高速输出饿死事件循环。
_READ_CHUNK = 65536
_READ_ROUNDS = 64
_READ_LIMIT = 512 * 1024
_READ_WINDOW = 0.002        # 合并窗口：把这段时间内陆续到达的数据攒成一笔
_READ_COALESCE_MIN = 16     # 首笔至少这么大才值得等待合并（按键回显仅 1-3 字节）


class Engine:
    """终端引擎。"""

    # 这两个 asyncio 原语在 run() 里创建，不在 __init__：3.8 的 Event / Queue
    # 会在构造时绑定当时的事件循环，而本类通常先于 asyncio.run() 构造。
    _stdin_q: asyncio.Queue
    _exit_event: asyncio.Event

    def __init__(
        self,
        backend: TerminalBackend,
        argv: list[str],
        screen: ScreenModel,
        handlers: dict[str, Handler],
        initial_handler: str,
    ) -> None:
        self._backend = backend
        self._argv = argv
        self.screen = screen
        self._handlers = handlers
        self._current = initial_handler
        self._pending_handler: str | None = None

        self._pty: PtySession | None = None
        self._pty_fd = -1
        self._console: Console | None = None
        self._printer: LinePrinter | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

        self._key_parser = KeyParser()
        self._key_buf: list[Key] = []

        self._exit_status: int | None = None
        self._exit_code: int | None = None

        self._editors: dict[str, LineEditor] = {}

        # 覆盖层几何：位置由模型实时光标推导，只记录当前帧的实际占用。
        self._active_prompt: tuple[str, str, int, int, int] | None = None
        self._overlay_anchor_row: int | None = None
        self._overlay_lines = 0
        self._overlay_timer: asyncio.TimerHandle | None = None

        # 等待喂给模型的字节（显示优先时，模型在后面追赶）。
        self._backlog = bytearray()

        self._services = Services(self)

    # ---- 只读属性 ----

    @property
    def exited(self) -> bool:
        return self._exit_event.is_set()

    @property
    def services(self) -> Services:
        return self._services

    def _passthrough(self) -> bool:
        """当前处理器是否要求「子进程输出原样透传」。

        由处理器自己声明（属性 ``passthrough``），引擎只读取。透传时显示优先、
        模型在后追赶；否则终端内容一律由模型投影而来。
        """
        handler = self._handlers.get(self._current)
        return bool(getattr(handler, "passthrough", False))

    # ---- 主入口 ----

    async def run(self) -> int:
        # 这两个原语必须**在运行中的循环里**创建：3.8 的 asyncio.Event / Queue
        # 会在构造时绑定当时的事件循环（3.10 起才改为惰性绑定）。本类通常在
        # asyncio.run() 之前就构造好了，在那里创建会绑到另一个循环上，之后
        # await 时报 "got Future attached to a different loop"。
        self._stdin_q = asyncio.Queue()
        self._exit_event = asyncio.Event()

        winsize = Winsize(rows=self.screen.rows, columns=self.screen.columns)
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
            self._loop.add_signal_handler(signal.SIGHUP, self._on_hangup)
            self._loop.add_signal_handler(signal.SIGTERM, self._on_terminate)
            self._loop.add_signal_handler(signal.SIGQUIT, self._on_quit)
            self._loop.add_signal_handler(signal.SIGINT, self._on_interrupt)
            self._loop.add_signal_handler(signal.SIGTSTP, self._on_suspend)
            self._loop.add_signal_handler(signal.SIGCONT, self._on_resume)
        except (NotImplementedError, RuntimeError):
            # 部分平台/非主线程不支持在事件循环里装信号处理器：退化为默认行为
            pass

        try:
            while not self._exit_event.is_set():
                handler = self._handlers.get(self._current)
                if handler is None:
                    # 处理器名字无效：退回注册表里的第一个，避免空转
                    self._current = next(iter(self._handlers))
                    handler = self._handlers[self._current]
                try:
                    await handler.step(self._services)
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001
                    # 任何未捕获异常都不能让 traceback 直接写到 stderr:
                    # 那些字节不经过显示逻辑，会撑滚终端并冲掉覆盖层。
                    try:
                        await self.show_message(f"error: {e}")
                    except Exception:  # noqa: BLE001
                        pass
                # 处理器可能请求了切换（协作式：本轮做完才切）
                if self._pending_handler is not None:
                    self._current = self._pending_handler
                    self._pending_handler = None
        finally:
            await self._cleanup()

        # 因宿主信号退出时用该信号对应的退出码（与 screen 一致）；
        # 否则反映子进程，这样 `lls sh -c 'exit 3'; echo $?` 才能拿到 3。
        if self._exit_code is not None:
            return self._exit_code
        return self._child_exit_code()

    def request_handler(self, name: str) -> None:
        """请求切换处理器（下一轮生效）。"""
        self._pending_handler = name

    # ---- 退出码 ----

    def _child_exit_code(self) -> int:
        """把子进程的结束方式翻译成退出码，规则与 shell 一致。

        正常退出取它的退出码；被信号终止取 ``128 + 信号值``。
        拿不到状态（子进程还在跑、或从未启动）时返回 0。
        """
        if self._exit_status is None:
            self._await_child_status()
        status = self._exit_status
        if status is None:
            return 0
        if os.WIFEXITED(status):
            return os.WEXITSTATUS(status)
        if os.WIFSIGNALED(status):
            return 128 + os.WTERMSIG(status)
        return 0

    def _await_child_status(self, timeout: float = 0.2) -> None:
        """极短暂地等子进程被回收，用于退出时拿到准确的退出码。

        PTY 的 EIO 可能先于 SIGCHLD 到达，此时主循环已结束、子进程却还没被
        ``waitpid`` 回收（``_reap`` 会拿到 ``(0, 0)``）。这里自己轮询几次即可，
        不依赖 SIGCHLD 处理器 —— 反正退出在即，阻塞这一下无所谓。
        """
        deadline = time.monotonic() + timeout
        while self._exit_status is None and time.monotonic() < deadline:
            self._reap()
            if self._exit_status is None:
                time.sleep(0.005)

    # ---- 启动对齐 ----

    def _align_model_to_cursor(self) -> None:
        """把模型光标对齐到宿主终端当前光标所在行（用 CUP，不用换行）。

        模型的初始状态是一张空屏、光标在 (0,0)，但用户进入时主机光标未必在顶行
        （最常见：上一个 shell 的提示符就停在屏幕底部）。若不对齐，模型认为内容
        从屏幕顶部开始，而物理上从中间/底部开始，两者差了一段行距。

        **必须用 CUP 定位，不能喂换行**：喂换行会让模型把这些行标成 dirty
        （"内容变了"），随后按模型重绘时就会用空白覆盖它们 —— 而物理上那几行
        是启动前留下的真实内容，模型根本没收到过，属于**未知区域**，
        绝不能碰（否则历史输出会被擦成空白）。

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
            self.screen.feed(f"\x1b[{row};{column}H".encode())
            # 对齐用的定位不该被当成"画面变化"
            self.screen.clear_dirty()

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

    # ---- fd 回调 ----

    def _on_master_readable(self) -> None:
        if self._exit_event.is_set():
            return
        data = self._read_all_available()
        if data is None:
            return
        if not data:
            self._on_child_exit()
            return
        if self._console is None:
            self.screen.feed(data)
            return
        if self._passthrough():
            # 透传：立刻写终端，模型排队追赶（见 _pump_backlog）。
            #
            # 解析是纯 Python，一次整屏重绘约 10ms；若在写终端前同步解析，
            # 事件循环就被占住、读不到下一批输出，画面只能一顿一顿地刷新。
            # 模型只是影子，晚几百毫秒无妨 —— 需要它时先排空积压。
            self._console.write(data)
            self._backlog += data
            self._pump_backlog()
        else:
            # 渲染模式：不写子进程字节，终端内容一律由模型投影而来。
            # 透传会让「子进程画的」与「覆盖层画的」争用同一个终端。
            self.flush_backlog()
            self.screen.feed(data)
            self.render_from_model()

    def _read_all_available(self) -> bytes | None:
        """在很短的窗口内读走子进程所有输出，**合并成一块**。

        子进程画一帧会分多次 write：全屏程序启动依次发「切换备用屏」「设置
        滚动区」「清屏」「逐行绘制」。逐笔转发时终端每笔刷新一次，于是画面
        先空、再半成品、最后完整——用户看到的就是「已经画出完整界面，随后又
        被清掉重画一遍」。

        因此在 :data:`_READ_WINDOW` 内把陆续到达的数据攒在一起再交出，
        让终端按帧刷新而不是按系统调用刷新。

        窗口是上限而非固定等待：数据连续到达时会一直读到 ``_READ_LIMIT``。
        只有**首笔够大**（全屏程序的一帧）才值得等后续；首笔很小（键盘回显、
        零星日志）时不等待，立刻交出，交互因此不受影响。
        读失败且已处理时返回 ``None``。
        """
        chunks: list[bytes] = []
        total = 0
        deadline = time.perf_counter() + _READ_WINDOW
        for _ in range(_READ_ROUNDS):
            try:
                chunk = os.read(self._pty_fd, _READ_CHUNK)
            except BlockingIOError:
                # 暂时读完。只有已经攒到「够一帧」的量，才值得等同一帧的其余部分；
                # 否则立即返回（否则每次按键都要白等一个窗口）。
                if total < _READ_COALESCE_MIN:
                    break
                remain = deadline - time.perf_counter()
                if remain <= 0:
                    break
                try:
                    ready, _, _ = select.select([self._pty_fd], [], [], remain)
                except (OSError, ValueError):
                    break
                if not ready:
                    break
                continue
            except OSError as e:
                if e.errno == errno.EIO:
                    # EIO 表示 PTY 已无更多数据（子进程退出后的正常结束）。
                    # 若本轮已经读到东西，**必须先交出去**再谈退出：直接走退出
                    # 路径会丢掉 chunks，表现为子进程最后几行输出凭空消失。
                    if chunks:
                        break
                    self._on_child_exit()
                    return None
                break
            if not chunk:
                if not chunks:
                    return b""
                break
            chunks.append(chunk)
            total += len(chunk)
            if total >= _READ_LIMIT:
                break
        return b"".join(chunks)

    def _pump_backlog(self) -> None:
        """解析一小片积压。

        每收到一批输出顺带解析一片，解析速度因此与产出速度挂钩。
        不做 `call_soon` 式的连续追赶：那会让事件循环排满解析回调，反而挤占
        读 pty 的时机，比同步解析还慢。落后太多时由 :data:`_BACKLOG_LIMIT`
        兜底一次喂完，保证模型不会无限期落后于子进程。
        """
        self._feed_backlog_slice()

    def _feed_backlog_slice(self) -> None:
        """喂一小片积压（按时间预算），并清掉 dirty。

        透传模式下物理终端已是真实画面，dirty 没有意义；不清的话，
        下次进入渲染模式会把这些「早就显示在屏幕上」的行再重绘一遍。
        """
        if not self._backlog:
            return
        if len(self._backlog) > _BACKLOG_LIMIT:
            # 积压过多：一次喂完。宁可卡一下，也不让模型与子进程脱节。
            self.screen.feed(bytes(self._backlog))
            self._backlog.clear()
            self.screen.clear_dirty()
            return
        started = time.perf_counter()
        cut = 0
        total = len(self._backlog)
        while cut < total:
            step = min(_FEED_SLICE, total - cut)
            self.screen.feed(bytes(self._backlog[cut:cut + step]))
            cut += step
            if time.perf_counter() - started >= _FEED_BUDGET:
                break
        del self._backlog[:cut]
        self.screen.clear_dirty()

    def flush_backlog(self) -> None:
        """把积压全部喂完，使模型与子进程一致。

        透传模式下模型在后追赶（见 ``_pump_backlog``），所以**任何**要用模型
        的地方都必须先排空：渲染、取光标定位覆盖层、切换到渲染模式。
        漏掉一处就会用上过时的光标：子进程早已把光标移到别处，而那次 CUP
        还压在 backlog 里，提示符因此会落在随机的旧行上。

        已排空时是空操作，可以随手调用。
        """
        if self._backlog:
            self.screen.feed(bytes(self._backlog))
            self._backlog.clear()
            self.screen.clear_dirty()

    # ---- 渲染 ----

    def render_from_model(
        self, *, keep_overlay: bool = True, leave_cursor: bool = True
    ) -> None:
        """把模型内容投影到物理终端。

        不写子进程的原始字节，只写「模型算出来的画面」：

        1. 覆盖层占用的行并入 dirty —— 它们既要腾出来，又要按模型显示，
           所以「让出覆盖层」和「重绘内容」是同一次写，不是两次；
        2. 重绘所有 dirty 行（pyte 精确标记变化的行）；
        3. 把物理光标放到模型光标处。

        这样物理终端恒等于模型，覆盖层是唯一浮在上面的东西。

        ``keep_overlay=False``：只让出画面，不重新调度覆盖层绘制。
        ``leave_cursor=False``：不动光标，调用方紧接着会自行定位。
        """
        if self._console is None:
            return
        # 渲染与光标定位都基于模型，因此模型必须已经完整。
        self.flush_backlog()
        dirty = set(self.screen.dirty)
        if self._overlay_anchor_row is not None and self._overlay_lines:
            top = self._overlay_anchor_row
            bottom = min(top + self._overlay_lines, self.screen.rows)
            dirty.update(range(top, bottom))
        self._overlay_anchor_row = None
        self._overlay_lines = 0
        self.screen.clear_dirty()

        if dirty:
            out = []
            for row in sorted(dirty):
                if not 0 <= row < self.screen.rows:
                    continue
                out.append(f"\x1b[{row + 1};1H")
                out.append(render_row_cells(self.screen.row_cells(row)))
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
        覆盖层的绘制不写模型，模型光标始终代表子进程的真实位置。
        """
        self._overlay_timer = None
        active = self._active_prompt
        if active is None or self._console is None:
            return
        prompt, text, cursor, _lines_all, _lines_cur = active
        _, new_all = self.draw_overlay(prompt + text, cursor)
        self._active_prompt = (prompt, text, cursor, new_all, new_all)

    # ---- 输入回调 ----

    def _on_stdin_readable(self) -> None:
        if self._exit_event.is_set():
            return
        try:
            data = os.read(sys.stdin.fileno(), 10240)
        except (OSError, BlockingIOError):
            return
        if data:
            self._stdin_q.put_nowait(data)

    def push_keys(self, data: bytes) -> None:
        if data:
            self._stdin_q.put_nowait(data)

    # ---- 信号 ----

    def _on_winch(self) -> None:
        try:
            size = os.get_terminal_size()
        except OSError:
            return
        self.screen.resize(size.lines, size.columns)
        if self._pty is not None:
            self._pty.resize(size.lines, size.columns)

    def _on_sigchld(self) -> None:
        """SIGCHLD：有子进程状态变化，但**未必**是 PTY 子进程。

        本进程还会有别的子进程（例如 HTTP 客户端发请求时执行的 ``uname -p``
        平台探测），它们退出同样会送来该信号。若不加区分地把任何 SIGCHLD 当成
        「终端会话结束」，一次普通的外部命令退出就会让整个会话误判为终止 ——
        正在读取输入的命令（如 ``generate``）会把它当成「用户中断」而静默放弃，
        真正的原因反而被吞掉。

        因此只有确认 PTY 子进程真的被 ``waitpid`` 回收，才算会话结束。
        """
        self._reap()
        if self._exit_status is not None:
            self._on_child_exit()

    def _on_hangup(self) -> None:
        """SIGHUP：宿主断开（SSH 掉线、窗口关闭）。

        screen 的对应处理是通知后端挂断后 ``exit(0)``。这里没有可驻留的后端，
        子进程是直接子进程，所以必须先把它一并结束，否则会变成孤儿继续跑。
        退出码按 screen 用 0。
        """
        self._forward_and_quit(signal.SIGHUP, 0)

    def _on_terminate(self) -> None:
        """SIGTERM：外部要求终止。screen 走 ``Finit(1)``，退出码为 1。"""
        self._forward_and_quit(signal.SIGTERM, 1)

    def _on_quit(self) -> None:
        """SIGQUIT：与 SIGTERM 同等处理（screen 里两者共用一个 handler）。"""
        self._forward_and_quit(signal.SIGQUIT, 1)

    def _forward_and_quit(self, signum: int, code: int) -> None:
        """把信号转给子进程组，然后让引擎以 ``code`` 退出。

        子进程经 ``setsid`` 成为独立会话，**收不到**发给本进程的信号；
        不转发的话它会被留在后台继续跑。
        """
        if self._exit_event.is_set():
            return
        self._exit_code = code
        if self._pty is not None:
            self._pty.signal(signum)
        self._on_child_exit()

    def _on_interrupt(self) -> None:
        """SIGINT：转发给子进程组，**自己继续运行**。

        这是与 screen 一致的关键一条：Ctrl-C 的语义是打断窗口里的前台作业，
        不是杀掉多路复用器本身。terminal 产生的 SIGINT 本就会直接送达子进程的
        前台进程组，这里处理的是**别人直接发给本进程**的那一种。
        """
        if self._exit_event.is_set() or self._pty is None:
            return
        self._pty.signal(signal.SIGINT)

    def _on_suspend(self) -> None:
        """SIGTSTP：交还终端后停住自己。

        不交还终端的话，挂起期间用户面对一个 raw 模式的终端：没有回显、
        Ctrl-C 也不生效。恢复时 :meth:`_on_resume` 重新接管。

        这里用 ``SIGSTOP`` 而不是把 SIGTSTP 改回默认处置再自发一次：后者会覆盖
        asyncio 注册的处理器，恢复后第二次挂起就不再经过本方法，终端也就不会被
        交还。``SIGSTOP`` 本身不可捕获，效果相同且不动处理器。
        """
        if self._console is None:
            return
        suspend = getattr(self._backend, "suspend_console", None)
        if suspend is not None:
            suspend()
        os.kill(os.getpid(), signal.SIGSTOP)

    def _on_resume(self) -> None:
        """SIGCONT：重新接管终端。

        挂起期间终端已交还给 shell，必须重新进入 raw 模式，否则输入不会被我们
        读到。挂起期间子进程的输出积在 pty 缓冲里，恢复后会经 ``_on_master_readable``
        正常到达并触发相应重绘，这里只需把尚未解析的积压补上，使模型与终端一致。
        """
        if self._console is None:
            return
        resume = getattr(self._backend, "resume_console", None)
        if resume is not None:
            resume()
        self.flush_backlog()

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
        """回收子进程并记下它的 wait 状态。

        ``waitpid(WNOHANG)`` 在子进程还没结束时返回 ``(0, 0)``，必须按 pid
        判断，否则会把「还没退出」当成状态存起来。
        """
        if self._pty is None:
            return
        try:
            wpid, status = os.waitpid(self._pty.pid, os.WNOHANG)
        except (ChildProcessError, OSError):
            return
        if wpid == self._pty.pid and self._exit_status is None:
            self._exit_status = status

    # ---- 终端写入（Services 转发目标）----

    def write_child(self, data: bytes, *, enter: bool = False) -> None:
        """写子进程；``enter`` 为 True 时追加换行（若末尾尚无换行）。"""
        if enter and not data.endswith(b"\n"):
            data += b"\n"
        self._pty_write(data)

    def _pty_write(self, data: bytes) -> None:
        if self._exit_event.is_set() or self._pty is None:
            return
        try:
            self._pty.write(data)
        except OSError as e:
            if e.errno == errno.EIO:
                self._on_child_exit()

    async def show_message(self, text: str) -> None:
        """以覆盖层显示消息，等任意键后收起。不改变处理器。

        子进程已退出时不等待按键（否则会一直卡在这里）。
        """
        assert self._printer is not None
        self.draw_overlay(text)
        try:
            if not self._exit_event.is_set():
                await self.read_key()
        finally:
            self.restore()

    def restore(self) -> None:
        """收起覆盖层，按模型还原它占的行，并把物理光标定位到模型光标。"""
        self.render_from_model(keep_overlay=False)

    # ---- 覆盖层 ----

    def draw_overlay(self, text: str, cursor: int | None = None) -> tuple[int, int]:
        """绘制覆盖层内容，自约束位置。

        覆盖层（命令行、消息、预览）都浮在画面上，是临时显示。它若不自己约束
        位置，画到屏幕底之外就会让终端滚动、把上方内容顶走，物理画面与屏幕
        模型随之错位 —— 之后子进程的回显会被写进错误的行/列。

        因此这里由覆盖层自己负责：

        1. 算出需要几行、上一帧占了哪几行，按**行区间**精确擦除（位置可能变）；
        2. 用 :func:`overlay_anchor` 定出起始行：靠近基准行，放不下就整体上移；
        3. 以绝对定位绘制。

        **不写模型**：覆盖层是浮动层，不是子进程的画面。模型光标始终是子进程
        的真实位置。

        **位置只取模型的实时光标**，不记住「进入时的位置」：覆盖层是浮动的，
        就该贴着子进程当前所在的行。

        ``cursor`` 为 None 时表示纯展示，光标停在文末。
        返回 ``(anchor, lines_all)``。
        """
        assert self._printer is not None
        # 位置取自模型光标，因此模型必须已经完整。透传模式下模型在后追赶，
        # 这里先排空再取光标；已排空时是空操作。
        self.flush_backlog()
        if self._overlay_timer is not None:
            # 即将重绘，取消挂起的延迟重绘，避免同一帧画两次
            self._overlay_timer.cancel()
            self._overlay_timer = None
        width = self.screen.columns
        _, lines_needed = wrap_multi_lines(text, width)
        anchor = overlay_anchor(self.screen.cursor.y, lines_needed, self.screen.rows)
        # 「让出旧位置」只在真的换位置时才做：
        # 若新锚点与旧锚点完全相同，先恢复模型内容再立刻被覆盖是纯粹的重复写。
        # 位置没变时直接覆盖即可。
        if self._overlay_anchor_row != anchor:
            # 让出旧位置（并入 dirty 一次写回），不重画覆盖层、不动光标
            # —— 紧接着 print_lines 会画到新锚点
            self.render_from_model(keep_overlay=False, leave_cursor=False)
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
        c = self.screen.cursor
        self._console.write(f"\x1b[{c.y + 1};{c.x + 1}H".encode())

    # ---- 行读取 ----

    async def read_line(
        self,
        prompt: str = ":",
        *,
        include_last: bool = False,
        value: str = "",
        cancel: str | None = None,
        exit: str | None = None,
        history_id: str = "_default",
        escape: Any = None,
        keep_history: bool = True,
    ) -> Any:
        """读一行输入，完整行编辑。

        返回：输入内容；Ctrl-C → ``cancel``；Ctrl-D → ``exit``；
        Esc 且 ``escape`` 非 None → ``escape``；子进程退出 → None。

        ``keep_history`` 为 False 时，这一行不进入该 ``history_id`` 的历史。
        """
        editor = self._editors.get(history_id)
        if editor is None:
            editor = LineEditor()
            self._editors[history_id] = editor
        editor.reset(value)

        assert self._printer is not None
        anchor, lines_all = self.draw_overlay(
            prompt + editor.text, len(prompt) + editor.cursor
        )
        lines_cur = lines_all
        # **立刻**登记活跃覆盖层，而不是等到第一次按键之后。
        # 否则「刚进入、用户在等、子进程此时输出」的窗口里 _active_prompt
        # 仍是 None，输出会直接透传并把覆盖层冲掉。
        self._active_prompt = (
            prompt,
            editor.text,
            len(prompt) + editor.cursor,
            lines_all,
            lines_cur,
        )
        try:
            while True:
                key = await self.read_key()
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
                    self.render_from_model(keep_overlay=False)
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
                        return editor.submit(keep=keep_history)
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
                anchor, lines_all = self.draw_overlay(
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
            # 覆盖层不再显示，后续输出无需再绕过它。
            self._active_prompt = None

    def record_history(self, value: str, history_id: str) -> None:
        """把一条内容记入某 ``history_id`` 的历史，供之后用 ↑ 召回。

        只写历史，不改变当前编辑状态。给那些不是经提示行输入、却需要事后
        召回的内容使用。
        """
        editor = self._editors.get(history_id)
        if editor is None:
            editor = LineEditor()
            self._editors[history_id] = editor
        editor.record(value)

    def requeue_pending_keys(self) -> None:
        """把尚在键缓冲里的按键回填到输入队列。

        用于退出覆盖模式时：用户可能在同一个字节块里先按了 Esc（退出）又敲了
        别的字；这些字节属于新的上下文（通常要交给子进程），直接丢弃会表现为
        「按键失灵」。
        """
        if not self._key_buf:
            return
        data = b"".join(k.raw for k in self._key_buf)
        self._key_buf.clear()
        if data:
            self._stdin_q.put_nowait(data)

    # ---- 键读取 ----

    async def read_key(self) -> Key | None:
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

    async def read_chars(self, timeout: float | None = None) -> bytes | None:
        """读一段原始输入字节（不解析按键）。超时返回空字节，退出返回 None。"""
        data = await self._wait_stdin(timeout)
        return data

    async def _wait_stdin(self, timeout: float | None = None) -> bytes | None:
        """等一段 stdin 字节。子进程退出返回 None，超时返回 b""。"""
        if timeout is not None:
            if self._stdin_q.empty():
                get_task = asyncio.ensure_future(self._stdin_q.get())
                exit_task = asyncio.ensure_future(self._exit_event.wait())
                done, pending = await asyncio.wait(
                    {get_task, exit_task},
                    timeout=timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for t in pending:
                    t.cancel()
                if get_task in done:
                    return get_task.result()
                if exit_task in done:
                    return None
                return b""
        get_task = asyncio.ensure_future(self._stdin_q.get())
        exit_task = asyncio.ensure_future(self._exit_event.wait())
        done, pending = await asyncio.wait(
            {get_task, exit_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for t in pending:
            t.cancel()
        if get_task in done:
            return get_task.result()
        return None

