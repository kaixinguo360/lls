"""终端后端：按平台选择实现。"""

from __future__ import annotations

import sys

from .backend import Console, Key, PtySession, TerminalBackend, Winsize
from .keys import KEY_NAMES, KeyNameError, KeyParser, encode_key, encode_keys

__all__ = [
    "Console",
    "KEY_NAMES",
    "Key",
    "KeyNameError",
    "KeyParser",
    "PtySession",
    "TerminalBackend",
    "Winsize",
    "encode_key",
    "encode_keys",
    "make_backend",
]


def make_backend() -> TerminalBackend:
    """按运行平台选择后端。"""
    if sys.platform in ("linux", "darwin"):
        from .posix import PosixBackend

        return PosixBackend()
    from .unsupported import UnsupportedBackend

    return UnsupportedBackend(platform=sys.platform)
