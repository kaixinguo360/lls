"""终端后端：按平台选择实现。"""

from __future__ import annotations

import sys

from .backend import Console, Key, PtySession, TerminalBackend, Winsize
from .keys import KeyParser

__all__ = [
    "Console",
    "Key",
    "KeyParser",
    "PtySession",
    "TerminalBackend",
    "Winsize",
    "make_backend",
]


def make_backend() -> TerminalBackend:
    """按运行平台选择后端。"""
    if sys.platform in ("linux", "darwin"):
        from .posix import PosixBackend

        return PosixBackend()
    from .unsupported import UnsupportedBackend

    return UnsupportedBackend(platform=sys.platform)
