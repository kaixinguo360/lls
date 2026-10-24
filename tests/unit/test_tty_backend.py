"""终端后端选择：按模块能力判断，而不是平台名。

``sys.platform`` 的取值随解释器构建方式变化，按平台名判断会把能跑的
环境误判成不支持。这里守住「判据是模块可用性」。
"""

from __future__ import annotations

import os

import pytest

from lls import tty
from lls.tty.unsupported import UnsupportedBackend


def _modules_available() -> bool:
    if os.name != "posix":
        return False
    try:
        import fcntl  # noqa: F401
        import pty  # noqa: F401
        import termios  # noqa: F401
    except ImportError:
        return False
    return True


def test_probe_follows_module_availability() -> None:
    assert tty.supports_posix_terminal() is _modules_available()


def test_missing_modules_fall_back_to_the_unsupported_backend(monkeypatch) -> None:
    monkeypatch.setattr(tty, "supports_posix_terminal", lambda: False)

    backend = tty.make_backend()

    assert isinstance(backend, UnsupportedBackend)
    with pytest.raises(RuntimeError, match="POSIX"):
        backend.open_console()


@pytest.mark.skipif(not _modules_available(), reason="POSIX tty modules unavailable")
def test_available_modules_use_the_posix_backend() -> None:
    from lls.tty.posix import PosixBackend

    assert isinstance(tty.make_backend(), PosixBackend)


def test_doctor_reports_the_probe(capsys) -> None:
    from lls.cli import doctor

    assert doctor() == 0
    assert "posix tty:" in capsys.readouterr().out
