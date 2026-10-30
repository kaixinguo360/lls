"""透传输出通路：子进程字节与覆盖层必须**同一帧**下发。

透传时终端由子进程的字节直接画，它会把覆盖层冲掉，所以每写完一笔就把
覆盖层按它自己记的锚点补回去，两件事写在同一笔里（否则终端会在中间刷新
一次，看到的是「覆盖层被冲掉」的那半帧）。

本文件守住新通路的三个不变量：

1. 子进程字节与覆盖层补画落在**同一次** console.write 里（有覆盖层时）；
2. 补画前先让模型追上，覆盖层贴着子进程光标走，让出来的旧行还给模型；
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
    alternate = False           # 引擎按它判断子进程是否换了屏

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


def test_a_row_is_cleared_before_it_is_painted() -> None:
    """整行写满时必须先清行，再写内容。

    写完一行最后一列，终端把光标停在那个格子并进入「待折行」状态；此时再发
    ``EL``（``ESC [ 0K``）是「擦掉光标所在格及其右侧」—— 刚写上去的最后一个
    字符被自己的清行擦掉。用户看到的就是「最右边那个字有时候不显示」，而
    「刚好写满」取决于当时的列宽，所以只是有时候。
    """
    from lls.term.model import Cell

    row = [Cell("A") for _ in range(80)]
    painted = engine_mod._row_paint(row)
    assert painted.startswith("\x1b[0K"), "清行必须在内容之前"
    assert painted.endswith("A"), "内容整段跟在清行之后"
    assert "\x1b[0K" not in painted[1:], "一行只清一次，且不落在内容后面"


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


def test_repaint_follows_the_child_cursor_and_gives_the_old_row_back() -> None:
    """补画不是原地再画一遍：子进程挪到哪，覆盖层跟到哪，旧行还给模型。"""
    engine, screen, console = _engine()
    engine.draw_overlay(":abc", 4)
    assert engine.overlay.anchor == 3

    screen.cursor = SimpleNamespace(y=20, x=5)  # 子进程写到了下面
    console.writes.clear()

    engine._repaint_overlay()

    out = console.writes[0]
    assert engine.overlay.anchor == 20
    assert b"\x1b[21;1H" in out   # 覆盖层画在新位置
    assert b"\x1b[4;1H" in out    # 让出来的那一行按模型重铺


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
