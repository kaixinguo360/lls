"""终端工具的观察几何、括号粘贴与 ``resize_terminal``。

观察里那四个属性（``rows``/``cols``/``cursor``/``buffer``）是模型判断「我在
看什么」的唯一来源；粘贴只在子进程要了 ``?2004`` 且文本含换行时才走括号
粘贴 —— 单行必须是逐键，否则 vim 里的 ``i`` 会变成插入一个字母。
"""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

from lls.ai.tools import builtin_tools
from lls.handlers import PassthroughHandler
from lls.session.engine import Engine


class FakeConsole:
    def __init__(self) -> None:
        self.writes: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.writes.append(bytes(data))

    def close(self) -> None:  # pragma: no cover - 仅满足后端接口
        pass

    def text(self) -> str:
        return b"".join(self.writes).decode("latin-1")


class FakeScreen:
    columns = 80
    rows = 24
    alternate = False
    host_modes: frozenset[int] = frozenset()

    def __init__(self) -> None:
        self.cursor = SimpleNamespace(x=2, y=4, hidden=False)
        self.dirty: set[int] = set()
        self.title = ""
        self.cursor_style = None
        self.clipboard = None
        self.resized: list[tuple[int, int]] = []

    def text(self, **kwargs) -> str:
        return "hello\nworld"

    def clear_dirty(self) -> None:
        self.dirty.clear()

    def row_cells(self, row: int) -> list:
        return []

    def resize(self, rows: int, columns: int) -> None:
        self.resized.append((rows, columns))
        self.rows, self.columns = rows, columns
        self.dirty.update(range(rows))


class FakePort:
    def __init__(self, screen: FakeScreen | None = None) -> None:
        self.screen = screen or FakeScreen()
        self.written: list[tuple[bytes, bool]] = []
        self.resized: list[tuple[int, int]] = []

    def write(self, data: bytes, *, enter: bool = False) -> None:
        self.written.append((data, enter))

    def flush_model(self) -> None:
        pass

    async def wait(self, timeout: float) -> None:
        await asyncio.sleep(0)

    def resize(self, rows: int, columns: int) -> tuple[int, int]:
        self.resized.append((rows, columns))
        return rows, columns


class FakeBackend:
    def close_console(self) -> None:
        pass


def _entries(port: FakePort) -> dict:
    return {item.tool.name: item for item in builtin_tools(port, port.wait)}


# ---- 观察几何 ----


def test_the_observation_carries_geometry() -> None:
    port = FakePort()

    out = asyncio.run(
        _entries(port)["read_terminal"].run({"lines": 10, "wait_seconds": 0})
    )

    assert '<terminal_state rows="24" cols="80" cursor="5,3" buffer="main">' in out


def test_the_buffer_says_when_a_full_screen_program_owns_it() -> None:
    screen = FakeScreen()
    screen.alternate = True
    port = FakePort(screen)

    out = asyncio.run(
        _entries(port)["read_terminal"].run({"lines": 10, "wait_seconds": 0})
    )

    assert 'buffer="alternate"' in out


# ---- 括号粘贴 ----


def test_multiline_text_is_pasted_when_the_child_asked_for_it() -> None:
    port = FakePort()
    port.screen.host_modes = frozenset({2004})

    out = asyncio.run(
        _entries(port)["send_text"].run({"text": "a\nb", "enter": True})
    )

    data, enter = port.written[-1]
    assert data == b"\x1b[200~a\nb\x1b[201~"
    assert enter is True
    assert "括号粘贴" in out


def test_a_single_line_is_never_pasted() -> None:
    """``send_text("i")`` 在 vim 里必须还是「进入插入模式」。"""
    port = FakePort()
    port.screen.host_modes = frozenset({2004})

    asyncio.run(_entries(port)["send_text"].run({"text": "i", "enter": False}))

    assert port.written[-1] == (b"i", False)


def test_without_the_mode_multiline_text_is_sent_as_keystrokes() -> None:
    port = FakePort()
    port.screen.host_modes = frozenset()

    asyncio.run(_entries(port)["send_text"].run({"text": "a\nb", "enter": False}))

    assert port.written[-1] == (b"a\nb", False)


# ---- resize_terminal ----


def test_resize_terminal_forwards_the_size() -> None:
    port = FakePort()

    out = asyncio.run(
        _entries(port)["resize_terminal"].run({"rows": 10, "columns": 40})
    )

    assert port.resized == [(10, 40)]
    assert "10×40" in out


def test_resize_terminal_says_when_the_host_was_smaller() -> None:
    port = FakePort()
    port.resize = lambda rows, columns: (24, 80)  # 宿主兜底的 80×24

    out = asyncio.run(
        _entries(port)["resize_terminal"].run({"rows": 500, "columns": 1000})
    )

    assert "24×80" in out
    assert "收窄" in out


# ---- 引擎侧 ----


def _engine(monkeypatch):
    monkeypatch.setattr(
        os, "get_terminal_size", lambda: os.terminal_size((80, 24))
    )
    screen = FakeScreen()
    engine = Engine(
        backend=FakeBackend(),  # type: ignore[arg-type]
        argv=[],
        screen=screen,  # type: ignore[arg-type]
        handlers={"default": PassthroughHandler({})},
        initial_handler="default",
    )
    console = FakeConsole()
    engine._attach_console(console)  # type: ignore[arg-type]
    pty = SimpleNamespace(resized=[], resize=lambda r, c: pty.resized.append((r, c)))
    engine._pty = pty  # type: ignore[assignment]
    return engine, screen, console, pty


def test_engine_resize_clamps_to_the_host(monkeypatch) -> None:
    engine, screen, _console, pty = _engine(monkeypatch)

    assert engine.resize(500, 1000) == (24, 80)
    assert screen.resized[-1] == (24, 80)
    assert pty.resized[-1] == (24, 80)


def test_shrinking_blanks_the_rows_the_model_left(monkeypatch) -> None:
    engine, _screen, console, _pty = _engine(monkeypatch)

    engine.resize(10, 40)

    # 第 10..23 行（1-based 11..24）各自「定位 + 擦行」。
    assert "\x1b[11;1H\x1b[0K" in console.text()
    assert "\x1b[24;1H\x1b[0K" in console.text()
