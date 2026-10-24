"""终端后端：按能力选择实现。

判据是 POSIX 终端所需的模块是否真的可用，而不是平台名字符串：
Windows 原生没有 ``pty`` / ``termios`` / ``fcntl``，POSIX 环境则有。
"""

from __future__ import annotations

import os
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
    "supports_posix_terminal",
]


def supports_posix_terminal() -> bool:
    """当前解释器是否具备 POSIX 终端所需的能力。

    ``sys.platform`` 的取值随解释器构建方式变化，只看平台名并不可靠；真正
    需要的是 ``fcntl`` / ``pty`` / ``termios`` 三个模块，因此按模块是否
    可导入判断。Windows 上这些模块一定不存在。
    """
    if os.name != "posix":
        return False
    try:
        import fcntl  # noqa: F401
        import pty  # noqa: F401
        import termios  # noqa: F401
    except ImportError:
        return False
    return True


def make_backend() -> TerminalBackend:
    """按运行平台选择后端。"""
    if supports_posix_terminal():
        from .posix import PosixBackend

        return PosixBackend()
    from .unsupported import UnsupportedBackend

    return UnsupportedBackend(platform=sys.platform)
