"""POSIX 终端后端：本项目唯一允许 import pty/termios/fcntl 的模块。

职责：

* ``spawn`` 用 ``pty.openpty`` 启动子进程，独占从端；
* ``open_console`` 把宿主终端切成 raw 模式；
* 处理 SIGWINCH 窗口变化。

绝不 import 任何 ui / commands / ai / term 模块，保持平台层纯净。
"""

from __future__ import annotations

import fcntl
import os
import pty
import signal
import struct
import sys
import termios
import tty
from typing import Callable

from .backend import Console, PtySession, TerminalBackend, Winsize

__all__ = ["PosixBackend"]


class _PosixPtySession:
    def __init__(self, master_fd: int, pid: int) -> None:
        self.master_fd = master_fd
        self._pid = pid
        self._callback: Callable[[bytes], None] | None = None

    @property
    def pid(self) -> int:
        return self._pid

    def fileno(self) -> int:
        return self.master_fd

    def write(self, data: bytes) -> None:
        if self.master_fd >= 0:
            os.write(self.master_fd, data)

    def on_output(self, cb: Callable[[bytes], None]) -> None:
        self._callback = cb

    def _deliver(self, data: bytes) -> None:
        if self._callback is not None:
            self._callback(data)

    def resize(self, rows: int, columns: int) -> None:
        if self.master_fd < 0:
            return
        winsize = struct.pack("HHHH", rows, columns, 0, 0)
        fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, winsize)

    def close(self) -> None:
        if self.master_fd >= 0:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = -1


class _PosixConsole:
    """宿主终端：直接写 stdin/stdout 的 fd。"""

    def write(self, data: bytes) -> None:
        os.write(sys.stdout.fileno(), data)


class PosixBackend:
    """Linux/macOS 终端后端。"""

    def __init__(self) -> None:
        self._old_tty: list[list[int | list[bytes | int]]] | None = None
        self._console: _PosixConsole | None = None

    def spawn(self, argv: list[str], winsize: Winsize) -> PtySession:
        master_fd, slave_fd = pty.openpty()
        _set_winsize(slave_fd, winsize.rows, winsize.columns)
        pid = os.fork()
        if pid == 0:
            # 子进程：会话首领，独占从端
            os.close(master_fd)
            os.setsid()
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

    def close_console(self) -> None:
        if self._old_tty is not None:
            try:
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._old_tty)
            except Exception:
                pass
            self._old_tty = None


def _set_winsize(fd: int, rows: int, columns: int) -> None:
    winsize = struct.pack("HHHH", rows, columns, 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
