"""agent_pager：整屏排版、滚动与按键。"""

from __future__ import annotations

import asyncio

from lls.commands.builtin.agent_pager import Pager, page
from lls.tty import Key, KeyParser


def _pager(lines, rows=10, columns=40, footer="状态行"):
    return Pager(lines, footer, rows=rows, columns=columns)


def _key(control="", char="", raw=b""):
    return Key(char=char, control=control, raw=raw)


def test_frame_is_exactly_one_screen_tall() -> None:
    pager = _pager(["a", "b"])
    screen = pager.frame().split("\n")
    assert len(screen) == 10
    assert screen[0] == "a"
    assert screen[-1].endswith("状态行\x1b[0m")


def test_long_lines_wrap_to_columns() -> None:
    pager = _pager(["x" * 85], columns=40)
    assert len(pager.lines) == 3


def test_opens_at_the_last_line() -> None:
    pager = _pager(["line {}".format(i) for i in range(100)])
    screen = pager.frame().split("\n")
    assert screen[0] == "line 91"
    assert screen[-2] == "line 99"
    assert pager.handle(_key(char="j")) == "none"
    assert pager.handle(_key(char="k")) == "redraw"
    assert pager.frame().split("\n")[0] == "line 90"


def test_line_keys_stop_at_both_ends() -> None:
    pager = _pager(["line {}".format(i) for i in range(100)])
    pager.first()
    assert pager.handle(_key(char="k")) == "none"
    assert pager.handle(_key(char="j")) == "redraw"
    assert pager.top == 1
    assert pager.handle(_key(char="G")) == "redraw"
    assert pager.top == len(pager.lines) - 9
    assert pager.handle(_key(char="j")) == "none"


def test_arrow_keys_move_one_line() -> None:
    pager = _pager(["line {}".format(i) for i in range(100)])
    pager.first()
    assert pager.handle(_key(control="down", raw=b"\x1b[B")) == "redraw"
    assert pager.top == 1
    assert pager.handle(_key(control="down", raw=b"\x1b[B")) == "redraw"
    assert pager.top == 2
    assert pager.handle(_key(control="up", raw=b"\x1b[A")) == "redraw"
    assert pager.top == 1


def test_page_and_home_end_keys() -> None:
    pager = _pager(["line {}".format(i) for i in range(100)])
    pager.first()
    assert pager.handle(_key(control="pagedown")) == "redraw"
    assert pager.top == 9
    assert pager.handle(_key(control="pageup")) == "redraw"
    assert pager.top == 0
    # PgUp/PgDn 不在解析器的已知按键表里，会以原始序列到达
    assert pager.handle(_key(control="terminal_response", raw=b"\x1b[6~")) == "redraw"
    assert pager.top == 9
    assert pager.handle(_key(control="end", raw=b"\x1b[F")) == "redraw"
    assert pager.top == len(pager.lines) - 9
    assert pager.handle(_key(control="home", raw=b"\x1b[H")) == "redraw"
    assert pager.top == 0


def test_space_enter_and_half_page() -> None:
    pager = _pager(["line {}".format(i) for i in range(100)])
    pager.first()
    assert pager.handle(_key(char=" ")) == "redraw"
    assert pager.top == 9
    assert pager.handle(_key(char="b")) == "redraw"
    assert pager.top == 0
    assert pager.handle(_key(control="enter")) == "redraw"
    assert pager.top == 1
    assert pager.handle(_key(char="d")) == "redraw"
    assert pager.top == 5
    assert pager.handle(_key(char="u")) == "redraw"
    assert pager.top == 1


def test_quit_keys() -> None:
    pager = _pager(["a"])
    assert pager.handle(_key(char="q")) == "quit"
    assert pager.handle(_key(char="Q")) == "quit"
    assert pager.handle(_key(control="escape", raw=b"\x1b")) == "quit"
    assert pager.handle(_key(control="ctrl_c", raw=b"\x03")) == "quit"
    assert pager.handle(_key(char="z")) == "none"


def test_an_arrow_split_across_reads_is_one_key() -> None:
    """终端常把 ES C [ / B 分两次送到：必须由解析器拼起来，否则按一次
    方向键会先被判成 Esc（收起），或者干脆没反应。"""
    parser = KeyParser()
    pager = _pager(["line {}".format(i) for i in range(100)])
    pager.first()
    assert parser.feed(b"\x1b[") == []
    keys = parser.feed(b"B")
    assert len(keys) == 1
    assert keys[0].control == "down"
    assert pager.handle(keys[0]) == "redraw"
    assert pager.top == 1


def test_a_lone_escape_is_quit_not_a_stray_sequence() -> None:
    parser = KeyParser()
    assert parser.feed(b"\x1b") == []
    key = parser.flush_escape()
    assert key is not None and key.control == "escape"
    assert _pager(["a"]).handle(key) == "quit"


def test_footer_keeps_one_line() -> None:
    pager = _pager(["a"], footer="标题  模型 \n 128/200 tok")
    assert pager.frame().split("\n")[-1].endswith("标题 模型 128/200 tok\x1b[0m")


def test_long_footer_is_clipped_to_one_line() -> None:
    pager = _pager(["a"], columns=20, footer="很长的标题 " * 5)
    screen = pager.frame().split("\n")
    assert len(screen) == 10
    assert screen[-1].endswith("…\x1b[0m")


def test_resize_keeps_one_screen() -> None:
    pager = _pager(["line {}".format(i) for i in range(50)])
    assert pager.resize(6, 30) is True
    assert len(pager.frame().split("\n")) == 6
    assert pager.resize(6, 30) is False


class FakeScreen:
    rows = 8
    columns = 20


class FakeServices:
    def __init__(self, keys=()) -> None:
        self.screen = FakeScreen()
        self._keys = list(keys)
        self.frames = []
        self.source = None
        self.restored = 0

    def draw_overlay(self, text, cursor=None):
        self.frames.append(text)
        return 0, len(text.split("\n"))

    def set_overlay_source(self, source=None) -> None:
        self.source = source

    async def read_key(self):
        return self._keys.pop(0) if self._keys else None

    def restore_context(self) -> None:
        self.restored += 1


def test_page_draws_one_screen_and_leaves_it_to_the_caller() -> None:
    services = FakeServices([_key(control="up", raw=b"\x1b[A"), _key(char="q")])
    asyncio.run(page(services, ["line {}".format(i) for i in range(50)], "底栏"))

    assert len(services.frames) == 2
    assert all(len(frame.split("\n")) == 8 for frame in services.frames)
    # 第一屏停在最后一行，按一次 ↑ 上移一行
    assert services.frames[0].split("\n")[0] == "line 43"
    assert services.frames[-1].split("\n")[0] == "line 42"
    # 这一屏不还给子进程：调用方拿它顶替（见 page / pick 的 finally）。
    assert services.restored == 0


def test_page_leaves_the_screen_when_the_child_goes_away() -> None:
    services = FakeServices([_key(control="down")])
    asyncio.run(page(services, ["a", "b"], "底栏"))
    # 这一屏不还给子进程：调用方拿它顶替（见 page / pick 的 finally）。
    assert services.restored == 0


def test_page_resizes_with_the_window() -> None:
    services = FakeServices([_key(control="down"), _key(char="q")])

    async def run() -> None:
        task = asyncio.ensure_future(page(services, ["a", "b"], "底栏"))
        await asyncio.sleep(0)
        services.screen.rows = 4
        await asyncio.sleep(0.3)
        await task

    asyncio.run(run())
    assert all(len(frame.split("\n")) == 4 for frame in services.frames[1:])


def test_page_registers_a_relayout_source_until_it_closes() -> None:
    """翻看器把「按新尺寸重排」交给引擎：窗口一变就立刻回调，不用等轮询。"""
    services = FakeServices([_key(char="q")])
    seen = {}

    async def run() -> None:
        task = asyncio.ensure_future(
            page(services, ["line {}".format(i) for i in range(50)], "底栏")
        )
        await asyncio.sleep(0)
        seen["source"] = services.source
        await task

    asyncio.run(run())

    source = seen["source"]
    assert source is not None
    # 出参是整屏：行数跟着新尺寸走，正文按新宽度重排
    assert len(source(4, 30).split("\n")) == 4
    assert len(source(12, 10).split("\n")) == 12
    assert services.source is None


def test_resize_stays_at_the_bottom_until_scrolled_up() -> None:
    pager = _pager(["line {}".format(i) for i in range(50)])
    pager.resize(20, 40)
    assert pager.frame().split("\n")[-2] == "line 49"
    pager.first()
    pager.resize(10, 40)
    assert pager.top == 0
