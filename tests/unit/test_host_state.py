"""标题、光标形状、剪贴板：渲染模式下由引擎转给宿主，透传时子进程自己发。

三者都是「子进程最近一次设置的值」，与上次转过的相同就一个字节都不写；
剪贴板还要过 ``terminal.clipboard`` 这道开关（默认关闭，是安全面）。
"""

from __future__ import annotations

from types import SimpleNamespace

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
        return b"".join(self.writes).decode("utf-8", "replace")


class FakeScreen:
    rows = 4
    columns = 10
    alternate = False
    host_modes = frozenset()

    def __init__(self) -> None:
        self.cursor = SimpleNamespace(x=0, y=0, hidden=False)
        self.dirty: set[int] = set()
        self.title = ""
        self.cursor_style = None
        self.clipboard = None

    def feed(self, data: bytes) -> None:
        pass

    def clear_dirty(self) -> None:
        self.dirty.clear()

    def row_cells(self, row: int) -> list:
        return []

    def resize(self, rows: int, columns: int) -> None:  # pragma: no cover
        pass


class RenderHandler:
    passthrough = False

    async def step(self, services) -> None:  # pragma: no cover - 用例不跑循环
        pass


def _engine(*, clipboard: bool = False):
    screen = FakeScreen()
    engine = Engine(
        backend=None,  # type: ignore[arg-type]
        argv=[],
        screen=screen,  # type: ignore[arg-type]
        handlers={"default": PassthroughHandler({}), "render": RenderHandler()},
        initial_handler="render",
        clipboard=clipboard,
    )
    console = FakeConsole()
    engine._attach_console(console)  # type: ignore[arg-type]
    return engine, screen, console


def test_the_title_is_forwarded_once() -> None:
    engine, screen, console = _engine()

    screen.title = "编译中"
    engine.render_from_model()
    engine.render_from_model()

    assert console.text().count("\x1b]0;编译中\x07") == 1


def test_a_cleared_title_is_forwarded_too() -> None:
    engine, screen, console = _engine()
    screen.title = "编译中"
    engine.render_from_model()

    screen.title = ""
    engine.render_from_model()

    assert console.text().endswith("\x1b]0;\x07\x1b[?2026l")


def test_nothing_is_forwarded_while_passthrough() -> None:
    """透传时子进程的序列直达宿主，引擎再发一遍就是重复。"""
    engine, screen, console = _engine()
    engine._current = "default"
    screen.title = "编译中"

    engine.render_from_model()

    assert console.text() == ""


def test_the_cursor_shape_is_forwarded_once() -> None:
    engine, screen, console = _engine()

    screen.cursor_style = 5
    engine.render_from_model()
    engine.render_from_model()

    assert console.text().count("\x1b[5 q") == 1


def test_the_clipboard_needs_the_switch() -> None:
    engine, screen, console = _engine(clipboard=False)
    screen.clipboard = "5L2g5aW9"

    engine.render_from_model()

    assert "52;" not in console.text()


def test_the_clipboard_is_forwarded_when_enabled() -> None:
    engine, screen, console = _engine(clipboard=True)
    screen.clipboard = "5L2g5aW9"

    engine.render_from_model()
    engine.render_from_model()

    assert console.text().count("\x1b]52;c;5L2g5aW9\x07") == 1
