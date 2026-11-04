"""宿主终端的上报模式：只在这几个交接点上被 LLS 改写。

子进程在透传里设的模式是**直接发给宿主**的（vim 发 ``?1000h``，宿主就开始
上报鼠标）。渲染模式接管键盘后，这些上报没有人接收，字节会落进命令行编辑器；
交还时要按**那一刻**模型里的模式补回，而不是进入时的快照 —— 子进程在渲染
期间照样会开关它们。
"""

from __future__ import annotations

import asyncio
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
        return b"".join(self.writes).decode("latin-1")


class FakeScreen:
    rows = 6
    columns = 20
    alternate = False

    def __init__(self) -> None:
        self.host_modes: frozenset[int] = frozenset()
        self.cursor = SimpleNamespace(x=0, y=0, hidden=False)
        self.dirty: set[int] = set()

    def feed(self, data: bytes) -> None:
        pass

    def clear_dirty(self) -> None:
        self.dirty.clear()

    def row_cells(self, row: int) -> list:
        return []

    def resize(self, rows: int, columns: int) -> None:  # pragma: no cover
        pass


class FakeBackend:
    def close_console(self) -> None:
        pass


class RenderHandler:
    """渲染模式的替身：只要 ``passthrough`` 为假，引擎就认为键盘归界面。"""

    passthrough = False

    async def step(self, services) -> None:  # pragma: no cover - 用例不跑循环
        pass


def _engine():
    screen = FakeScreen()
    engine = Engine(
        backend=FakeBackend(),  # type: ignore[arg-type]
        argv=[],
        screen=screen,  # type: ignore[arg-type]
        handlers={"default": PassthroughHandler({}), "render": RenderHandler()},
        initial_handler="default",
    )
    console = FakeConsole()
    engine._attach_console(console)  # type: ignore[arg-type]
    return engine, screen, console


def test_entering_render_mode_turns_host_reporting_off() -> None:
    """透传期间宿主与模型同步，所以进入时按模型就能算出「宿主现在开着什么」。"""
    engine, screen, console = _engine()
    screen.host_modes = frozenset({1000, 1006, 2004})

    engine._switch_handler("render")

    assert "?1000;1006;2004l" in console.text()


def test_leaving_render_mode_restores_what_the_child_wants_now() -> None:
    """期间子进程又开了焦点上报：模型里有，交还时就要补上。"""
    engine, screen, console = _engine()
    engine._switch_handler("render")
    console.writes.clear()

    screen.host_modes = frozenset({1004})
    engine._switch_handler("default")

    assert "?1004h" in console.text()


def test_a_mode_the_child_turned_off_is_not_restored() -> None:
    """进入渲染时宿主开着鼠标；期间子进程退了 vim（它自己发了 ``?1000l``），
    交还时不能再打开 —— 快照式恢复正是在这里出错的。"""
    engine, screen, console = _engine()
    screen.host_modes = frozenset({1000, 2004})
    engine._switch_handler("render")
    console.writes.clear()

    screen.host_modes = frozenset()
    engine._switch_handler("default")

    assert console.text() == ""


def test_a_mode_turned_off_and_on_again_is_restored() -> None:
    """期间关掉又打开：交还时按**最终**状态补回，而不是按进入时的快照。"""
    engine, screen, console = _engine()
    screen.host_modes = frozenset({1000})
    engine._switch_handler("render")          # 宿主被关掉
    console.writes.clear()

    screen.host_modes = frozenset()           # 期间关掉
    screen.host_modes = frozenset({1000})     # 又打开
    engine._switch_handler("default")

    assert "?1000h" in console.text()


def test_cleanup_resets_modes_without_the_child() -> None:
    """子进程可能先死：没人替它发 ``?1000l``，退出时必须自己收尾。"""
    engine, screen, console = _engine()
    engine._host_modes = {1000, 2004}

    asyncio.run(engine._cleanup())

    assert "?1000;2004l" in console.text()


def test_a_hidden_cursor_is_not_placed_and_comes_back() -> None:
    engine, screen, console = _engine()

    screen.cursor.hidden = True
    screen.dirty.add(0)
    engine.render_from_model()
    assert "\x1b[?25l" in console.text()

    console.writes.clear()
    screen.cursor.hidden = False
    screen.dirty.add(0)
    engine.render_from_model()
    assert "\x1b[?25h" in console.text()
