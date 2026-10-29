"""透传输出通路：子进程字节与覆盖层必须**同一帧**下发。

透传时终端由子进程的字节直接画，它会把覆盖层冲掉，所以每写完一笔就把
覆盖层按它自己记的锚点补回去，两件事写在同一笔里（否则终端会在中间刷新
一次，看到的是「覆盖层被冲掉」的那半帧）。

本文件守住新通路的三个不变量：

1. 子进程字节与覆盖层补画落在**同一次** console.write 里（有覆盖层时）；
2. 补画只认覆盖层自己的锚点，不看底部模型是不是已经追上；
3. 洪流下积压**有界**，且任何一次喂模型都不再把整份积压一次塞进去。
"""

from __future__ import annotations

from types import SimpleNamespace

from lls.handlers import PassthroughHandler
from lls.session import engine as engine_mod
from lls.session.engine import Engine


class FakeConsole:
    def __init__(self) -> None:
        self.writes: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.writes.append(bytes(data))

    def close(self) -> None:  # pragma: no cover - 仅满足后端接口
        pass


class FakeScreen:
    rows = 24
    columns = 80

    def __init__(self) -> None:
        self.fed = 0
        self.dirty: set[int] = set()
        self.cursor = SimpleNamespace(y=3, x=0)

    def feed(self, data: bytes) -> None:
        self.fed += len(data)
        self.dirty.add(0)

    def clear_dirty(self) -> None:
        self.dirty = set()

    def row_cells(self, row: int) -> list:
        return []

    def resize(self, rows: int, columns: int) -> None:  # pragma: no cover
        pass


class _Live:
    def is_set(self) -> bool:
        return False


def _engine():
    screen = FakeScreen()
    engine = Engine(
        backend=None,  # type: ignore[arg-type]
        argv=["sh"],
        screen=screen,  # type: ignore[arg-type]
        handlers={"default": PassthroughHandler({})},
        initial_handler="default",
    )
    console = FakeConsole()
    engine._exit_event = _Live()  # type: ignore[assignment]
    engine._attach_console(console)  # type: ignore[arg-type]
    return engine, screen, console


def test_passthrough_bytes_and_overlay_share_one_frame() -> None:
    """输出到达时，子进程字节与覆盖层补画写在同一笔里。"""
    engine, _screen, console = _engine()
    engine.draw_overlay(":abc", 4)
    console.writes.clear()
    engine._read_all_available = lambda: b"line-1\r\n"  # type: ignore[assignment]

    engine._on_master_readable()

    assert len(console.writes) == 1, "子进程字节与覆盖层被拆成了多笔"
    assert b"line-1\r\n" in console.writes[0]
    assert b":abc" in console.writes[0], "输出到达后没有把覆盖层补回去"


def test_overlay_repaints_at_its_own_anchor_not_the_lagging_model() -> None:
    """补画只认覆盖层自己记的锚点：模型还落在后面也不影响。"""
    engine, screen, console = _engine()
    engine.draw_overlay(":abc", 4)
    assert engine.overlay.anchor == 3

    screen.cursor = SimpleNamespace(y=20, x=5)  # 模型已被后来的输出带走
    console.writes.clear()

    engine._repaint_overlay()

    assert b"\x1b[4;1H" in console.writes[0]
    assert b"\x1b[21;1H" not in console.writes[0]


def test_flush_backlog_bounds_what_it_feeds() -> None:
    """洪流下 flush 不再把整份积压一次塞进模型。

    1MB+ 一次喂进 pyte 要两秒，那两秒里按键、画面全停 —— 用户按 Ctrl-E
    就是按了个空。丢历史对定位无害，模型只是影子。
    """
    engine, screen, _console = _engine()
    engine._backlog += b"x" * (engine_mod._BACKLOG_LIMIT * 4)

    engine.flush_backlog()

    assert screen.fed <= engine_mod._BACKLOG_KEEP
    assert engine._backlog == b""


def test_backlog_slice_keeps_backlog_bounded() -> None:
    """积压超限时按预算继续追，只丢历史，不整块喂完。"""
    engine, screen, _console = _engine()
    engine._backlog += b"x" * (engine_mod._BACKLOG_LIMIT * 4)

    engine._pump_backlog()

    assert len(engine._backlog) <= engine_mod._BACKLOG_LIMIT
    assert screen.fed < engine_mod._BACKLOG_LIMIT, "又整块喂进去了"
