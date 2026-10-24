"""PTY 会话与终端后端协议。

与具体平台无关的接口。实现方：

* :class:`~lls.tty.posix.PosixBackend` —— 唯一 import pty/termios/fcntl 的地方；
* ``FakeBackend``（测试）—— 用脚本化字节流驱动，不依赖真 pty。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

__all__ = ["Console", "Key", "PtySession", "TerminalBackend", "Winsize"]


@dataclass(frozen=True, slots=True)
class Winsize:
    rows: int
    columns: int


@dataclass(frozen=True, slots=True)
class Key:
    """一个按键事件。

    覆盖模式（line/prompt）下由 ``tty.keys`` 把字节流解析成 Key；
    透传模式（char）下不使用本类型，字节原样转发。
    """

    char: str = ""          # 可见字符（含宽字符）
    control: str = ""       # 控制键名："enter" | "escape" | "backspace" | "ctrl_e" ...
    raw: bytes = b""        # 原始字节序列（用于调试与精确还原）


class PtySession(Protocol):
    """一个已启动的子进程 PTY 会话。"""

    @property
    def pid(self) -> int: ...

    def fileno(self) -> int:
        """master fd，供 asyncio.add_reader 使用。"""
        ...

    def write(self, data: bytes) -> None:
        """向子进程写字节（不等待）。"""
        ...

    def resize(self, rows: int, columns: int) -> None:
        """调整 PTY 窗口大小。"""
        ...

    def signal(self, signum: int) -> None:
        """把信号发给子进程所在的整个进程组。"""
        ...

    def close(self) -> None:
        """关闭 PTY 与子进程。"""
        ...


class Console(Protocol):
    """宿主终端（用户看到的真实终端）的抽象。"""

    def write(self, data: bytes) -> None: ...


class TerminalBackend(Protocol):
    """平台终端后端：负责开 PTY、切宿主终端为 raw 模式。"""

    def spawn(self, argv: list[str], winsize: Winsize) -> PtySession:
        """启动子进程并返回 PTY 会话。"""
        ...

    def open_console(self) -> Console:
        """把宿主终端置为 raw 模式，返回可写字节的 Console。"""
        ...

    def suspend_console(self) -> None:
        """临时交还宿主终端（挂起用，保留原始属性以便恢复）。"""
        ...

    def resume_console(self) -> None:
        """从挂起恢复：重新进入 raw 模式。"""
        ...

    def close_console(self) -> None:
        """恢复宿主终端原有属性。"""
        ...
