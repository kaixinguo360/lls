"""不支持平台的后端：一被使用就报错，而不是让 import 在深处崩溃。"""

from __future__ import annotations

from typing import NoReturn

from .backend import Console, PtySession, Winsize

__all__ = ["UnsupportedBackend"]


class UnsupportedBackend:
    """没有 POSIX 终端的平台（Windows 原生）。"""

    def __init__(self, platform: str = "") -> None:
        self._platform = platform

    def _fail(self) -> NoReturn:
        raise RuntimeError(
            "LLS 需要 POSIX 终端（pty/termios/fcntl）。"
            f"当前平台 {self._platform or 'unknown'} 不受支持，请使用 WSL 或 Linux/macOS。"
        )

    def spawn(self, argv: list[str], winsize: Winsize) -> PtySession:
        self._fail()

    def open_console(self) -> Console:
        self._fail()

    def suspend_console(self) -> None:
        self._fail()

    def resume_console(self) -> None:
        self._fail()

    def close_console(self) -> None:
        self._fail()
