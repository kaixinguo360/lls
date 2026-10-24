"""不支持平台的后端：构造即报错，而不是让 import 在深处崩溃。"""

from __future__ import annotations

from .backend import Console, PtySession, Winsize

__all__ = ["UnsupportedBackend"]


class UnsupportedBackend:
    """Windows 等无 POSIX 终端的平台。"""

    def __init__(self, platform: str = "") -> None:
        self._platform = platform

    def _fail(self) -> None:
        raise RuntimeError(
            "LLS 需要 POSIX 终端（pty/termios/fcntl）。"
            f"当前平台 {self._platform or 'unknown'} 不受支持，请使用 WSL 或 Linux/macOS。"
        )

    def spawn(self, argv: list[str], winsize: Winsize) -> PtySession:
        self._fail()
        raise AssertionError("unreachable")

    def open_console(self) -> Console:
        self._fail()
        raise AssertionError("unreachable")

    def suspend_console(self) -> None:
        self._fail()

    def resume_console(self) -> None:
        self._fail()

    def close_console(self) -> None:
        self._fail()
