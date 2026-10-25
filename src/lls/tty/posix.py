"""POSIX 终端后端：本项目唯一允许 import pty/termios/fcntl 的模块。

职责：

* ``spawn`` 用 ``pty.openpty`` 启动子进程，子进程经 ``setsid`` + ``TIOCSCTTY``
  取得自己的控制终端（作业控制、SIGWINCH、Ctrl-C 都依赖它）；
* ``open_console`` 把宿主终端切成 raw 模式；
* ``_PosixPtySession.resize`` 把宿主尺寸变化转给子进程的 PTY；
  SIGWINCH 本身由 ``session.engine`` 监听，不在本模块。

绝不 import 任何 ui / commands / term 模块，保持平台层纯净。
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import pty
import re
import select
import struct
import sys
import termios
import time
import tty

from .backend import Console, PtySession, Winsize

# DSR（Device Status Report）应答：ESC [ 行 ; 列 R
_DSR_RE = re.compile(rb"\x1b\[(\d+);(\d+)R")

__all__ = ["PosixBackend"]


class _PosixPtySession:
    def __init__(self, master_fd: int, pid: int) -> None:
        self.master_fd = master_fd
        self._pid = pid

    @property
    def pid(self) -> int:
        return self._pid

    def fileno(self) -> int:
        return self.master_fd

    def write(self, data: bytes) -> None:
        if self.master_fd >= 0:
            os.write(self.master_fd, data)

    def resize(self, rows: int, columns: int) -> None:
        if self.master_fd < 0:
            return
        # PTY 已关闭（子进程刚退出）时改尺寸没有意义
        with contextlib.suppress(OSError):
            _apply_winsize(self.master_fd, rows, columns)

    def signal(self, signum: int) -> None:
        """把信号发给子进程所在的整个进程组。

        子进程启动时 ``setsid`` 成为新会话首领，因此 pgid 就是它的 pid。
        发给整个进程组才能覆盖它派生的前台作业（shell 里的 vim、管道等）——
        只发给 shell 本身，那些子作业收不到，与 screen 的行为也不一致。
        """
        if self._pid <= 0:
            return
        # 子进程已退出，或已不是我们能发信号的进程组
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(self._pid, signum)

    def close(self) -> None:
        if self.master_fd >= 0:
            with contextlib.suppress(OSError):
                os.close(self.master_fd)
            self.master_fd = -1


class _PosixConsole:
    """宿主终端：直接写 stdin/stdout 的 fd。"""

    def __init__(self) -> None:
        self._pending = b""

    def write(self, data: bytes) -> None:
        os.write(sys.stdout.fileno(), data)

    def cursor_position(self, timeout: float = 0.25) -> tuple[int, int] | None:
        """查询宿主终端光标位置（DSR：``ESC[6n`` → ``ESC[行;列R``）。

        返回 1-based ``(row, column)``；终端不响应（重定向/不支持）时返回 None。
        期间读到的非应答字节（用户提前敲的键）存入 ``_pending``，
        由 :meth:`take_pending` 取回，避免丢键。
        """
        fd = sys.stdin.fileno()
        os.write(sys.stdout.fileno(), b"\x1b[6n")
        buf = b""
        deadline = time.monotonic() + timeout
        while True:
            remain = deadline - time.monotonic()
            if remain <= 0:
                break
            try:
                ready, _, _ = select.select([fd], [], [], remain)
            except InterruptedError:
                continue
            if not ready:
                break
            try:
                chunk = os.read(fd, 256)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            match = _DSR_RE.search(buf)
            if match:
                self._pending = buf[: match.start()] + buf[match.end() :]
                return int(match.group(1)), int(match.group(2))
        self._pending = buf
        return None

    def take_pending(self) -> bytes:
        """取走查询期间读到的、属于用户输入的字节。"""
        data, self._pending = self._pending, b""
        return data


class PosixBackend:
    """Linux/macOS 终端后端。"""

    def __init__(self) -> None:
        self._old_tty: list[list[int | list[bytes | int]]] | None = None
        self._console: _PosixConsole | None = None

    def spawn(self, argv: list[str], winsize: Winsize) -> PtySession:
        master_fd, slave_fd = pty.openpty()
        _apply_winsize(slave_fd, winsize.rows, winsize.columns)
        pid = os.fork()
        if pid == 0:
            # 子进程：新建会话，并把从端设为自己的控制终端。
            #
            # setsid 只让进程成为「无控制终端的会话首领」；必须再 TIOCSCTTY
            # 才能把从端认作控制终端 —— 只做 dup2 不算「打开」，内核不会据此
            # 建立控制终端关系。没有这一步，子进程里：
            #   * 作业控制不可用（shell 报 "can't access tty; job control
            #     turned off"），Ctrl-Z / fg / bg 都失效；
            #   * 终端尺寸变化时内核不会给它发 SIGWINCH，全屏程序因此收不到
            #     窗口变化（vim 不会按新宽度重排）；
            #   * 终端的 Ctrl-C 等信号无法送达它的前台进程组。
            os.close(master_fd)
            os.setsid()
            try:
                fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
            except OSError as e:
                # 不静默降级：没有控制终端的会话能跑但失去作业控制与信号，
                # 症状很难排查，不如直接报告。
                os.write(2, f"lls: cannot set controlling tty: {e}\r\n".encode())
                os._exit(126)
            os.dup2(slave_fd, 0)
            os.dup2(slave_fd, 1)
            os.dup2(slave_fd, 2)
            if slave_fd > 2:
                os.close(slave_fd)
            try:
                os.execvp(argv[0], argv)
            except OSError as e:
                os.write(2, f"lls: exec failed: {e}\r\n".encode())
                os._exit(127)
        os.close(slave_fd)
        os.set_blocking(master_fd, False)
        return _PosixPtySession(master_fd, pid)

    def open_console(self) -> Console:
        fd = sys.stdin.fileno()
        self._old_tty = termios.tcgetattr(fd)
        tty.setraw(fd)
        self._console = _PosixConsole()
        return self._console

    def suspend_console(self) -> None:
        """临时交还宿主终端（挂起期间让用户能正常操作终端）。

        与 ``close_console`` 的区别：保留 ``_old_tty``，以便 :meth:`resume_console`
        重新接管。挂起若不交还终端，用户面对的是一个 raw 模式的终端，
        输入完全没有回显、Ctrl-C 也不生效。
        """
        if self._old_tty is None:
            return
        with contextlib.suppress(Exception):
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._old_tty)

    def resume_console(self) -> None:
        """从挂起恢复：重新进入 raw 模式。"""
        if self._old_tty is None:
            return
        with contextlib.suppress(Exception):
            tty.setraw(sys.stdin.fileno())

    def close_console(self) -> None:
        if self._old_tty is not None:
            with contextlib.suppress(Exception):
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._old_tty)
            self._old_tty = None


def _apply_winsize(fd: int, rows: int, columns: int) -> None:
    """把窗口尺寸写给 ``fd`` 指向的 PTY。

    对 master 端调用即可：内核会同步到对应的从端。PTY 双方共用同一个
    ``winsize``，所以启动时设从端、后续变化时设 master 端，效果一致。
    """
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))
