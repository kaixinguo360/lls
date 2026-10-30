"""显示原语单元测试：换行、覆盖层定位、按模型渲染。"""

from __future__ import annotations

import asyncio
from collections import deque

import pyte

from lls.session.engine import Engine, _PendingWrites
from lls.term import PyteScreenModel
from lls.session.overlay import Overlay
from lls.ui import (
    char_width,
    clusters,
    conservative_char_width,
    text_width,
    wrap_multi_lines,
)


class FakeConsole:
    def __init__(self) -> None:
        self.data = bytearray()
        self.writes = 0

    def write(self, data: bytes) -> None:
        self.data.extend(data)
        self.writes += 1

    def snapshot(self) -> bytes:
        out = bytes(self.data)
        self.data.clear()
        return out


# ---- char_width / wrap_multi_lines ----

def test_char_width() -> None:
    assert char_width("a") == 1
    assert char_width("中") == 2


def test_vs16_makes_a_narrow_char_wide() -> None:
    """U+2764 + VS16 是 emoji 呈现：终端画两列，逐字符相加只会算出一列。"""
    assert char_width("\u2764") == 1
    assert char_width("\u2764\ufe0f") == 2
    assert text_width("a\u2764\ufe0fb") == 4


def test_zero_width_marks_join_their_base() -> None:
    """组合符、ZWJ、肤色修饰符都归到基字符那一簇，宽度只算基字符的。"""
    assert list(clusters("e\u0301")) == ["e\u0301"]
    family = "\U0001f468\u200d\U0001f469\u200d\U0001f467"
    assert list(clusters(family)) == [family]
    assert text_width(family) == 2
    assert text_width("\U0001f44d\U0001f3fb") == 2


def test_regional_indicators_pair_into_one_cluster() -> None:
    assert list(clusters("\U0001f1e8\U0001f1f3")) == ["\U0001f1e8\U0001f1f3"]
    assert text_width("\U0001f1e8\U0001f1f3") == 2


def test_control_chars_never_join_the_previous_cluster() -> None:
    """ESC 这类控制符零列，但不是修饰符：并进上一簇会把转义序列拆散。"""
    assert list(clusters("a\x1bb")) == ["a", "\x1b", "b"]


def test_ambiguous_width_leaves_marks_alone() -> None:
    """保守宽度只放大真正占列的字符：组合符跟着基字符走。"""
    assert conservative_char_width("±") == 2
    assert conservative_char_width("e\u0301") == 1


def test_wrap_never_splits_a_cluster() -> None:
    """折行以簇为单位：基字符与其修饰符必须留在同一行。"""
    out, lines = wrap_multi_lines("a\u2764\ufe0fb", 2)

    rows = out.split("\r\n")
    assert lines == len(rows) == 3
    assert all(text_width(row) <= 2 for row in rows)
    # VS16 永远跟着它的基字符：单独出现在行尾或行首都说明被切开了。
    assert all("\ufe0f" not in row or "\u2764" in row for row in rows)
    assert all("\u2764" not in row or "\ufe0f" in row for row in rows)


def test_wrap_multi_lines() -> None:
    out, lines = wrap_multi_lines("123456789", 8)
    assert lines == 2
    assert "\r\n" in out


def test_wrap_counts_sgr_as_zero_width() -> None:
    """加粗（SGR）不占列：折行位置与不带样式时一致。"""
    plain, plain_lines = wrap_multi_lines("123456789", 8)
    styled, styled_lines = wrap_multi_lines("\x1b[1m123456789\x1b[0m", 8)

    assert styled_lines == plain_lines == 2
    assert styled.replace("\x1b[1m", "").replace("\x1b[0m", "") == plain


def test_wrap_replays_style_on_each_line() -> None:
    """每行自带样式的开与合：按行绝对定位重画时不依赖画行顺序。"""
    out, lines = wrap_multi_lines("\x1b[1m" + "ab" * 6 + "\x1b[0m", 4)

    rows = out.split("\r\n")
    assert lines == len(rows) > 1
    assert all(row.startswith("\x1b[1m") and row.endswith("\x1b[0m") for row in rows)


# ---- Overlay：覆盖层自己的模型 ----

def _row_text(ov: Overlay, row: int) -> str:
    return "".join(c.data for c in ov.row_cells(row)).rstrip()


def test_overlay_wraps_at_the_width() -> None:
    ov = Overlay(rows=4, columns=4)
    ov.set("abcdefgh")

    assert ov.lines == 2
    assert _row_text(ov, 0) == "abcd"
    assert _row_text(ov, 1) == "efgh"


def test_overlay_cursor_is_the_insertion_point() -> None:
    ov = Overlay(rows=4, columns=10)
    ov.set("> abcd", cursor=4)

    c = ov.cursor()
    assert c is not None
    assert (c.y, c.x) == (ov.anchor, 4)


def test_overlay_without_insertion_point_puts_the_cursor_at_the_end() -> None:
    """消息框、翻看器不指位置：光标落在它们写完的地方 —— 最上层说了算。"""
    ov = Overlay(rows=4, columns=10)
    ov.set("err\nboom")

    c = ov.cursor()
    assert c is not None
    assert (c.y, c.x) == (ov.anchor + 1, 4)


def test_overlay_keeps_sgr_style() -> None:
    ov = Overlay(rows=4, columns=10)
    ov.set("\x1b[1mhi\x1b[0m")

    cells = ov.row_cells(0)
    assert cells[0].data == "h" and cells[0].bold is True
    assert cells[2].bold is False


def test_overlay_does_not_count_trailing_blank_rows() -> None:
    ov = Overlay(rows=6, columns=10)
    ov.set("a\nb\nc\n\n\n")

    assert ov.lines == 3


def test_overlay_clear_forgets_the_content() -> None:
    ov = Overlay(rows=6, columns=10)
    ov.set("hello")
    ov.clear()

    assert ov.visible is False
    assert ov.text is None
    assert ov.anchor is None


# ---- 覆盖层变矮：多出来的旧行必须按模型恢复 ----

def _overlay_engine(rows: int = 8, columns: int = 20):
    screen = PyteScreenModel(rows=rows, columns=columns)
    console = FakeConsole()
    engine = Engine(
        backend=None,  # type: ignore[arg-type]
        argv=[],
        screen=screen,
        handlers={},
        initial_handler="default",
    )
    engine._attach_console(console)
    engine._loop = None
    return engine, screen, console


def _emulate(data: bytes, rows: int = 8, columns: int = 20):
    screen = pyte.Screen(columns, rows)
    pyte.ByteStream(screen).feed(data)
    return [line.rstrip() for line in screen.display]


def test_shrinking_overlay_restores_the_rows_it_used_to_cover() -> None:
    engine, screen, console = _overlay_engine()
    screen.feed(b"one\r\ntwo\r\nthree")

    engine.draw_overlay("a\nb\nc")  # 3 行，占模型光标起的 3 行
    console.snapshot()              # 丢掉第一帧
    engine.draw_overlay("x")        # 1 行：旧覆盖层的其余行要还原成模型内容

    rows = _emulate(bytes(console.data))
    assert rows[2].rstrip() == "x"
    assert rows[3].rstrip() == ""
    assert rows[4].rstrip() == ""


def test_overlay_follows_the_child_cursor_without_any_keypress() -> None:
    """子进程写完东西光标下移，覆盖层要跟着走 —— 不能等用户敲一个键才归位。"""
    engine, screen, console = _overlay_engine(rows=6, columns=20)
    screen.feed(b"one")
    engine.draw_overlay(":abc", cursor=4)   # 此刻光标在第 0 行
    console.snapshot()

    # 覆盖层画出来之后子进程才写：它的输出还在半路上，光标往下挪了三行。
    screen.feed(b"\r\ntwo\r\nthree\r\n")
    engine.render_from_model()

    rows = _emulate(bytes(console.data), rows=6, columns=20)
    assert [r.rstrip() for r in rows[:4]] == ["one", "two", "three", ":abc"]
    assert engine.overlay.anchor == 3


def test_backspace_clears_the_old_tail_of_a_wrapped_prompt() -> None:
    """退格后旧字符不能留在折行后的末行上（用户直接看到的症状）。"""
    engine, _screen, console = _overlay_engine(rows=6, columns=8)

    engine.draw_overlay("> abcdefgh", cursor=10)
    first = console.snapshot()
    engine.draw_overlay("> abcdefg", cursor=9)
    second = console.snapshot()

    rows = _emulate(first + second, rows=6, columns=8)
    assert rows[0].rstrip() == "> abcdef"
    assert rows[1].rstrip() == "g"



# ---- 一帧一笔：帧内的写攒到最外层退出时统一发出 ----

def test_overlay_takes_one_write_even_when_it_also_restores_the_model() -> None:
    """画一帧只该下发一次。

    覆盖层变矮时这一帧里既有「按模型恢复旧行」，又有「按新内容画
行」，
    分次写终端就会看到中间态：旧内容先被擦掉、新内容过一会儿才到。
    """
    engine, screen, console = _overlay_engine()
    screen.feed(b"one\r\ntwo\r\nthree")
    engine.draw_overlay("a\nb\nc")
    console.snapshot()
    console.writes = 0

    engine.draw_overlay("x")

    assert console.writes == 1


def test_passthrough_writes_are_not_held_back() -> None:
    """帧外的写直接下去：透传的字节自有其时序，不该被攒着。"""
    engine, _screen, console = _overlay_engine()
    engine._out.write(b"raw")

    assert console.data == b"raw"


# ---- 窗口尺寸变化：覆盖层按新宽度重排 ----

def test_resize_redraws_the_overlay_at_the_new_width() -> None:
    engine, screen, console = _overlay_engine(rows=6, columns=20)
    screen.feed(b"one")
    text = ">" + "a" * 18  # 20 列下是一行
    engine.draw_overlay(text)
    assert engine.overlay.lines == 1
    console.snapshot()

    screen.resize(6, 10)  # 宿主终端变窄
    engine._relayout_overlay()

    rows = _emulate(console.snapshot(), rows=6, columns=10)
    assert engine.overlay.lines == 2
    assert rows[0].rstrip() == ">aaaaaaaaa"
    assert rows[1].rstrip() == "aaaaaaaaa"


# ---- 纯展示的覆盖层也要跟着窗口尺寸重排 ----

def test_message_overlay_reflows_when_the_window_narrows() -> None:
    """消息框（show_message）不再随尺寸消失：按新宽度重画同一段文本。"""
    engine, _screen, console = _overlay_engine(rows=8, columns=40)
    text = "y" * 38
    engine.draw_overlay(text)
    assert engine.overlay.lines == 1
    console.snapshot()

    engine.screen.resize(8, 20)
    engine._relayout_overlay()

    rows = _emulate(console.snapshot(), rows=8, columns=20)
    assert engine.overlay.lines == 2
    assert rows[0].rstrip() == "y" * 20
    assert rows[1].rstrip() == "y" * 18


def test_overlay_source_relays_out_at_the_new_size() -> None:
    """自己排版的界面（翻看器）按引擎给的新行列数重排，而不是二次折行。"""
    engine, _screen, console = _overlay_engine(rows=8, columns=20)
    seen = []

    def source(rows: int, columns: int) -> str:
        seen.append((rows, columns))
        return "\n".join(["F{}x{}".format(rows, columns)] * rows)

    engine.set_overlay_source(source)
    console.snapshot()
    engine.screen.resize(5, 30)
    engine._relayout_overlay()

    assert seen == [(5, 30)]
    rows = _emulate(console.snapshot(), rows=8, columns=30)
    assert [r.rstrip() for r in rows[:5]] == ["F5x30"] * 5


def test_restore_forgets_the_overlay_so_it_is_not_drawn_again() -> None:
    """收起之后再改尺寸，不能把已经收起的覆盖层又画回来。"""
    engine, _screen, console = _overlay_engine(rows=8, columns=20)
    engine.draw_overlay("err: gone")
    engine.restore()
    console.snapshot()

    engine.screen.resize(8, 10)
    engine._relayout_overlay()

    assert b"gone" not in console.snapshot()


def test_a_tall_message_stops_at_the_bottom_of_the_screen() -> None:
    """比屏幕还高的覆盖层截在屏底：写到屏外会叠在末行上，看着像乱码。"""
    engine, _screen, console = _overlay_engine(rows=6, columns=20)

    engine.draw_overlay("\n".join("line {}".format(i) for i in range(12)))

    out = console.snapshot()
    assert engine.overlay.lines == 6
    assert b"\x1b[6;1H" in out
    assert b"\x1b[7;1H" not in out


def test_message_overlay_owns_the_physical_cursor() -> None:
    """消息框不指光标位置，物理光标仍归它：落在框内写完的地方。"""
    engine, screen, console = _overlay_engine(rows=8, columns=20)
    screen.feed(b"one\r\ntwo")
    console.snapshot()
    engine.draw_overlay("err")

    assert b"\x1b[2;4H" in console.snapshot()


# ---- 全屏程序（备用屏）：与主屏同一套行为 ----

VIM_ENTER = b"\x1b[?1049h\x1b[2J\x1b[HA hello\x1b[2;1H~"
VIM_LEAVE = b"\x1b[?1049l"


def test_fullscreen_app_keeps_the_overlay() -> None:
    """换屏不是覆盖层消失的理由：vim 进来后照旧合成在同一帧里。"""
    engine, screen, console = _overlay_engine()
    screen.feed(b"$ vim a.txt")
    engine.draw_overlay("(agent 1) planning")
    console.snapshot()

    screen.feed(VIM_ENTER)
    engine.render_from_model()

    assert screen.alternate is True
    assert engine.overlay.visible is True
    rows = _emulate(console.snapshot())
    assert any("planning" in row for row in rows)


def test_leaving_a_fullscreen_app_keeps_the_overlay() -> None:
    """退回主屏后同样还在：这一层不区分主屏与备用屏。"""
    engine, screen, console = _overlay_engine()
    screen.feed(b"$ vim a.txt")
    engine.draw_overlay("(agent 1) planning")
    screen.feed(VIM_ENTER)
    engine.render_from_model()
    console.snapshot()

    screen.feed(VIM_LEAVE)
    engine.render_from_model()

    assert screen.alternate is False
    assert engine.overlay.visible is True
    rows = _emulate(console.snapshot())
    assert any("planning" in row for row in rows)


def test_overlay_drawn_inside_a_fullscreen_app_is_kept() -> None:
    """在 vim 里画的（消息框、选择框）画在换屏之后，照旧留着。"""
    engine, screen, console = _overlay_engine()
    screen.feed(VIM_ENTER)
    engine.render_from_model()
    console.snapshot()

    engine.draw_overlay("小工具")
    engine.render_from_model()

    assert engine.overlay.visible is True
    rows = _emulate(console.snapshot())
    assert any("小工具" in row for row in rows)


def _passthrough_engine():
    class _Pass:
        passthrough = True

    screen = PyteScreenModel(rows=8, columns=20)
    console = FakeConsole()
    engine = Engine(
        backend=None,  # type: ignore[arg-type]
        argv=[],
        screen=screen,
        handlers={"pass": _Pass()},
        initial_handler="pass",
    )
    engine._attach_console(console)
    return engine, screen, console


class _Never:
    """``_exit_event`` 的替身：没有子进程退出，也不需要事件循环。"""

    def is_set(self) -> bool:
        return False


def test_passthrough_repaints_overlay_in_fullscreen_app() -> None:
    """透传时子进程字节照原样下去，覆盖层照旧补在它上面 —— 和主屏一个规矩。"""
    engine, screen, console = _passthrough_engine()
    engine._exit_event = _Never()
    pending: list[bytes] = []
    engine._read_all_available = lambda: pending.pop(0) if pending else None

    screen.feed(b"$ vim a.txt")
    engine.draw_overlay("(agent 1) planning")
    console.snapshot()

    pending.append(VIM_ENTER + b"\x1b[3;5Hx")
    engine._on_master_readable()
    out = console.snapshot()

    assert screen.alternate is True
    assert engine.overlay.visible is True
    assert b"planning" in out
    assert VIM_ENTER in out

# ---- 读输入后把覆盖层原地交给下一个界面 ----

def _loop_engine():
    """3.8 上 Event 构造时就绑定循环，只能在运行中的循环里建。"""
    engine, screen, console = _overlay_engine()
    engine._stdin_buf = deque()
    engine._stdin_ready = asyncio.Event()
    engine._exit_event = asyncio.Event()
    return engine, screen, console


def _typed(keys: bytes, handoff=None):
    """敲一串键跑一次 read_line，回放整段写后得到的屏幕。"""

    async def run():
        engine, screen, console = _loop_engine()
        screen.feed(b"one\r\ntwo\r\nthree")
        engine.push_keys(keys)
        line = await engine.read_line("> ", handoff=handoff)
        return engine, console, line

    engine, console, line = asyncio.run(run())
    return engine, _emulate(bytes(console.data)), line


def test_handoff_keeps_the_prompt_rows_for_the_next_screen() -> None:
    """按回车进 /status 时不还原：翻看界面直接顶替提示占的行。

    旧行为是 read_line 返回前按模型重绘一次，于是斜杠命令的下一屏到来
    之前会闪出一帧子进程画面。
    """
    def takes_over(text: str) -> bool:
        return text.startswith("/")

    engine, rows, line = _typed(b"/s\r", handoff=takes_over)

    assert line == "/s"
    # 提示行原地留着，没被子进程的画面顶掉
    assert rows[2].rstrip() == "> /s"
    assert engine.overlay.anchor is not None


def test_no_handoff_gives_the_rows_back_like_before() -> None:
    """成了任务的行照旧还原：接下来是子进程的输出。"""
    def takes_over(text: str) -> bool:
        return text.startswith("/")

    engine, rows, line = _typed(b"list files\r", handoff=takes_over)

    assert line == "list files"
    assert rows[0].rstrip() == "one"
    assert rows[2].rstrip() == "three"
    assert engine.overlay.anchor is None


def test_a_prompt_that_ends_with_the_child_gives_its_rows_back() -> None:
    """子进程退出不走 _leave：占的行也得还回模型，不能留在屏上。

    旧写法在 finally 里只 clear()，不把旧行记成待重铺，于是提示行那几行
    要等下一次别的重画才会归位 —— 子进程已经走了，那一次可能永远不来。
    """

    async def run():
        engine, screen, console = _loop_engine()
        screen.feed(b"one\r\ntwo\r\nthree")
        console.snapshot()            # 丢掉喂模型前的那一帧
        engine._exit_event.set()      # 读键时子进程已不在
        return engine, console, await engine.read_line("> ")

    engine, console, line = asyncio.run(run())

    assert line is None
    assert engine.overlay.anchor is None
    assert engine.overlay.lines == 0
    rows = _emulate(bytes(console.data))
    assert rows[2].rstrip() == "three"    # 提示行占的那一行还原成模型内容


# ---- 哪些行会换成另一个覆盖层 ----

def test_slash_lines_are_handed_over() -> None:
    from lls.commands.builtin.agent import _hands_over

    assert _hands_over("/status") is True
    assert _hands_over("/s") is True
    assert _hands_over("//原样交给模型") is True
    # 成了任务的行也立刻换成状态覆盖层（_Painter.begin 马上画上去）：
    # 先还原再重画，中间那一帧是空白。
    assert _hands_over("看看 README") is True
    assert _hands_over("") is True
    # 取消、退出这些哨兵值不进这一轮，行照旧还给子进程
    assert _hands_over(None) is False


# ---- 覆盖层跟着子进程光标走：输出流动时它跟着挪 ----

def test_the_overlay_follows_the_output_instead_of_staying_behind() -> None:
    """子进程往下写，覆盖层跟着挪；它让出来的那行还给模型。"""
    engine, screen, console = _overlay_engine()
    screen.feed(b"one\r\ntwo")
    engine.draw_overlay("(agent 1) planning")
    console.snapshot()
    console.writes = 0

    screen.feed(b"\r\nthree")
    engine.render_from_model()

    rows = _emulate(console.snapshot())
    assert console.writes == 1                  # 铺模型与补画覆盖层同属一帧
    assert engine.overlay.anchor == 2
    assert rows[1].rstrip() == "two"            # 旧行还给模型
    assert rows[2].rstrip() == "(agent 1) planning"


def test_the_cursor_lands_inside_the_overlay_after_it_moves() -> None:
    """挪过之后光标仍归浮层：落在它自己写完的地方。"""
    engine, screen, console = _overlay_engine()
    screen.feed(b"one\r\ntwo")
    engine.draw_overlay("err: something went wrong")
    console.snapshot()

    screen.feed(b"\r\nthree")
    engine.render_from_model()

    painted = pyte.Screen(20, 8)
    pyte.ByteStream(painted).feed(console.snapshot())
    own = engine.overlay.cursor()
    assert own is not None
    assert (painted.cursor.y, painted.cursor.x) == (own.y, own.x)
    assert painted.cursor.y >= engine.overlay.anchor


def test_output_without_any_overlay_draws_nothing_extra() -> None:
    """没有覆盖层时每帧都别白算一次（否则每次输出都多画一遍）。"""
    engine, screen, console = _overlay_engine()
    screen.feed(b"hello")
    engine.render_from_model()
    console.snapshot()

    engine.render_from_model()

    assert console.data == b""


def test_an_overlay_owns_the_physical_cursor() -> None:
    """浮层在，光标就归它：最上面那一层说了算，不借子进程的光标。"""
    engine, screen, console = _overlay_engine()
    screen.feed(b"one\r\ntwo\r\nthree")
    console.snapshot()

    engine.draw_overlay("err: boom")

    emulated = pyte.Screen(20, 8)
    pyte.ByteStream(emulated).feed(bytes(console.data))
    assert (emulated.cursor.y, emulated.cursor.x) == (2, 9)  # 框内写完的地方

def test_a_batch_of_keys_repaints_the_prompt_once() -> None:
    """同一批到达的键只重画一帧。

    每处理一个键就重画一次时，这些帧会逐个上屏：连按方向键，中间帧里的
    光标还没跟到后面几个键该在的位置，看起来就是光标在错处一闪。
    """
    cursors = []

    async def run():
        engine, screen, _console = _loop_engine()
        screen.feed(b"one\r\ntwo\r\nthree")
        real = engine.draw_overlay

        def counting(text, cursor=None):
            cursors.append(cursor)
            return real(text, cursor)

        engine.draw_overlay = counting
        engine.push_keys(b"abcd" + b"\x1b[D" * 6 + b"\r")
        return await engine.read_line("> ")

    assert asyncio.run(run()) == "abcd"
    assert len(cursors) == 1  # 只有进入时那一帧，按键不再逐个重画

def test_a_frame_is_written_between_sync_markers() -> None:
    """一帧整段夹在同步输出里：终端攒到收尾符才呈现，不会画出半帧。"""
    console = FakeConsole()
    out = _PendingWrites()
    out.attach(console)

    with out.frame():
        out.write(b"a")
        out.write(b"b")

    assert console.snapshot() == b"\x1b[?2026hab\x1b[?2026l"
