"""透传输出通路：子进程字节与覆盖层必须**同一帧**下发。

覆盖层浮在物理画面上，子进程的字节会把它冲掉。旧做法是「等输出静默
``OVERLAY_REDRAW_DELAY`` 再补画」，而每来一批输出都会把定时器取消重排 ——
持续输出时（间隔短于该延迟）永远轮不到重画，覆盖层只在间隙里闪一下。
所以这里改成：先把这一批喂给模型，再把「子进程字节 + 覆盖层」写成一笔。

本文件守住新通路的三个不变量：

1. 子进程字节与覆盖层重画落在**同一次** console.write 里（有覆盖层时）；
2. 模型还没追上（积压非空）时不按过时光标定锚，退回静默兜底；
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
    engine._console = console  # type: ignore[assignment]
    engine._out.attach(console)  # type: ignore[arg-type]
    return engine, screen, console


def test_passthrough_bytes_and_overlay_share_one_frame() -> None:
    """输出到达时，子进程字节与覆盖层重画写在同一笔里。

    分两笔的话，终端会在中间刷新一次 —— 那一帧上覆盖层是被冲掉的，
    持续输出时看起来就是「覆盖层一直在闪」。
    """
    engine, _screen, console = _engine()
    drawn: list[str] = []
    engine._active_prompt = (":", "abc", 4, 1, 1)  # type: ignore[assignment]
    engine.draw_overlay = (  # type: ignore[assignment]
        lambda text, cursor=None: drawn.append(text) or (3, 1)
    )
    engine._read_all_available = lambda: b"line-1\r\n"  # type: ignore[assignment]

    engine._on_master_readable()

    assert console.writes == [b"line-1\r\n"], "子进程字节与覆盖层被拆成了多笔"
    assert drawn == [":abc"], "输出到达后没有把覆盖层补回去"


def test_overlay_skipped_while_model_lags() -> None:
    """模型还落后（积压非空）时不立刻重画，改为静默兜底。

    此刻的光标属于上一批数据，按它定锚会把覆盖层画到过时的行列上。
    """
    engine, _screen, console = _engine()
    engine._active_prompt = (":", "", 1, 1, 1)  # type: ignore[assignment]
    engine._backlog += b"x" * 10
    scheduled: list[float] = []

    class FakeLoop:
        def call_later(self, delay, callback):
            scheduled.append(delay)
            return SimpleNamespace(cancel=lambda: None)

    engine._loop = FakeLoop()  # type: ignore[assignment]
    drawn: list[str] = []
    engine.draw_overlay = (  # type: ignore[assignment]
        lambda text, cursor=None: drawn.append(text) or (3, 1)
    )

    engine._redraw_overlay_now()

    assert drawn == [], "模型落后时仍按过时光标画了覆盖层"
    assert scheduled == [engine_mod.OVERLAY_REDRAW_DELAY], "没有退回静默兜底"


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
