"""回放状态机、屏幕绘制与 asciicast 导出。"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from lls.recording import Frame, Recording, RecordingError, TerminalRecorder
from lls.recording.replay import Playback, ScreenPainter, export_asciicast
from lls.term import PyteScreenModel


def _screen_factory(rows: int, columns: int) -> PyteScreenModel:
    return PyteScreenModel(rows=rows, columns=columns)


class _FakeRecording:
    def __init__(self, frames: list[Frame], geometry: tuple[int, int] = (5, 20)) -> None:
        self._frames = frames
        self.geometry = geometry

    def frames(self):
        return iter(self._frames)

    def duration(self) -> float:
        return self._frames[-1].at if self._frames else 0.0


def test_status_line_fits_by_display_width() -> None:
    from lls.recording.replay.tui import _display_width, _fit_line

    text = (
        "20261105T031000Z-1a2b3c4d  12.3/45.6s  1.0x  播放"
        "  [空格]暂停 [←→]5s [q]退出"
    )
    for width in (0, 1, 10, 40, 79):
        line = _fit_line(text, width)
        assert _display_width(line) == width
        assert "\n" not in line


def test_status_line_never_splits_a_wide_character() -> None:
    from lls.recording.replay.tui import _fit_line

    assert _fit_line("中中中", 3) == "中 "
    assert _fit_line("中中中", 4) == "中中"
    assert _fit_line("a\nb", 2) == "ab"


_STATUS_TEXT = (
    "20261105T031000Z-1a2b3c4d  12.3/45.6s  1.0x  播放"
    "  [空格]暂停 [←→]5s [q]退出"
)


def test_status_line_width_uses_the_same_rule_as_the_live_ui() -> None:
    """状态行的列宽与 ``lls.ui.width`` 同口径：模糊宽度算两列，整簇裁切。"""
    from lls.recording.replay.tui import _display_width, _fit_line
    from lls.ui.width import clusters, conservative_char_width, text_width

    samples = [
        "abc 123",
        "中中中",
        "←→·…“”±①",  # 模糊宽度：不少中日文终端画两列
        "❤️ done",  # VS16
        "👨‍👩‍👧‍👦 home",  # ZWJ 序列
        "🇨🇳 flag",  # 成对的区域指示符
        "e\u0301 a\u0308",  # 组合符
        "👍🏽 ok",  # 肤色修饰
    ]
    for sample in samples:
        assert _display_width(sample) == text_width(sample, conservative_char_width)
        prefixes = [""]
        for cluster in clusters(sample):
            prefixes.append(prefixes[-1] + cluster)
        for width in (0, 1, 2, 3, 5, 8, 13, 21):
            line = _fit_line(sample, width)
            assert _display_width(line) == width
            assert line.rstrip(" ") in prefixes, "裁切把一个簇切成了半个"


def test_drawing_the_status_line_never_wraps_the_last_row(monkeypatch) -> None:
    """状态行画在最后一行：多一格终端就自动折行，把整屏往上顶一行。"""
    import pyte

    from lls.recording.replay.tui import _fit_line
    from lls.ui.width import conservative_char_width

    # 宿主终端按「模糊宽度两列」排版（中日文终端就是这样，也是 lls 给 pyte
    # 注入的口径）；状态行若按 wcwidth 窄算，就会多出两格。
    monkeypatch.setattr(pyte.screens, "wcwidth", conservative_char_width)

    for columns in (30, 50, 60, 70, 80, 100, 120):
        screen = pyte.Screen(columns, 6)
        stream = pyte.Stream(screen)
        stream.feed("\x1b[H\x1b[2J")
        for row in range(6):
            stream.feed(f"\x1b[{row + 1};1HROW-{row}")
        before = [line.rstrip() for line in screen.display]

        line = _fit_line(_STATUS_TEXT, columns - 1)
        stream.feed("\x1b[6;1H\x1b[7m" + line + "\x1b[0m")
        after = [line.rstrip() for line in screen.display]

        assert after[:5] == before[:5], f"{columns} 列：画状态行把屏幕顶动了"
        assert after[5].rstrip() == line.rstrip()


def test_model_cursor_is_never_left_on_the_status_line() -> None:
    from lls.recording.replay.screen import Cursor
    from lls.recording.replay.tui import _cursor_sequence

    assert _cursor_sequence(Cursor(3, 4, False), 24, 80) == "\x1b[?25h\x1b[5;4H"
    # 模型光标在最后一行：那一行被状态行占着，藏起来而不是盖上去。
    assert _cursor_sequence(Cursor(0, 23, False), 24, 80) == "\x1b[?25l"
    assert _cursor_sequence(Cursor(0, 0, True), 24, 80) == "\x1b[?25l"


def test_key_decoder_recognizes_arrows_and_keeps_the_rest() -> None:
    from lls.recording.replay.tui import _decode_key

    assert _decode_key(b"\x1b[A") == ("up", b"")
    assert _decode_key(b"\x1b[Dq") == ("left", b"q")
    assert _decode_key(b"q") == ("q", b"")
    assert _decode_key(b"\x1b[") == (None, b"\x1b[")
    assert _decode_key(b"\x1bO") == (None, b"\x1bO")
    assert _decode_key(b"\x1b") == (None, b"\x1b")


def test_key_decoder_accepts_ss3_and_modified_arrows() -> None:
    from lls.recording.replay.tui import _decode_key

    # 应用的光标键模式（DECCKM）与 xterm 的修饰键形式都还是方向键。
    assert _decode_key(b"\x1bOA") == ("up", b"")
    assert _decode_key(b"\x1bOD") == ("left", b"")
    assert _decode_key(b"\x1b[1;5C") == ("right", b"")
    assert _decode_key(b"\x1b[1;2H") == ("home", b"")
    assert _decode_key(b"\x1b[1;5F") == ("end", b"")


def test_key_decoder_ignores_unknown_escapes_instead_of_quitting() -> None:
    from lls.recording.replay.tui import _decode_key

    # 焦点上报、SGR 鼠标、Alt+键 都不该被当成 Esc（那会让播放器直接退出）。
    assert _decode_key(b"\x1b[I") == ("ignore", b"")
    assert _decode_key(b"\x1b[<0;1;1M") == ("ignore", b"")
    assert _decode_key(b"\x1b[Z") == ("ignore", b"")
    assert _decode_key(b"\x1bq") == ("ignore", b"")
    # 连按两下 Esc 仍然退出。
    assert _decode_key(b"\x1b\x1b") == ("escape", b"\x1b")


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def monotonic(self) -> float:
        return self.now

    def wait(self, seconds: float) -> None:
        self.now += max(0.0, seconds)


class _Reads:
    """把 select/read/monotonic 换成可剧本化的版本。"""

    def __init__(self, monkeypatch, chunks) -> None:
        from lls.recording.replay import tui

        self.chunks = list(chunks)
        self.clock = _Clock()
        monkeypatch.setattr(tui.select, "select", self._select)
        monkeypatch.setattr(tui.os, "read", self._read)
        monkeypatch.setattr(tui.time, "monotonic", self.clock.monotonic)

    def _select(self, read, write, error, timeout):
        if self.chunks:
            return (list(read), [], [])
        self.clock.wait(timeout)
        return ([], [], [])

    def _read(self, fd, size):
        if not self.chunks:
            raise OSError("eof")
        return self.chunks.pop(0)


def _reader():
    from lls.recording.replay.tui import _Player

    player = object.__new__(_Player)
    player._keybuf = b""
    player._terminal = type("_Term", (), {"fd": 0})()
    return player


def test_read_key_waits_for_a_split_arrow(monkeypatch) -> None:
    _Reads(monkeypatch, [b"\x1b", b"[A"])

    assert _reader()._read_key(0.2) == "up"


def test_read_key_does_not_quit_on_an_esc_left_at_the_read_boundary(monkeypatch) -> None:
    """64 字节的 read 会切开方向键序列；留在 keybuf 里的 ESC 不能当退出。"""
    _Reads(monkeypatch, [b"[A"])
    player = _reader()
    player._keybuf = b"\x1b"

    assert player._read_key(0.0) == "up"
    assert player._keybuf == b""


def test_read_key_survives_the_sixty_four_byte_boundary(monkeypatch) -> None:
    # 连续 22 个方向键（66 字节）：第一读只拿 64 字节，正好切在最后一个 ESC 后。
    _Reads(monkeypatch, [b"\x1b[A" * 21 + b"\x1b", b"[A"])
    player = _reader()

    assert [player._read_key(0.0) for _ in range(22)] == ["up"] * 22


def test_read_key_treats_a_lone_esc_as_escape(monkeypatch) -> None:
    _Reads(monkeypatch, [b"\x1b"])

    assert _reader()._read_key(0.2) == "escape"


def test_read_key_ignores_an_unknown_complete_sequence(monkeypatch) -> None:
    _Reads(monkeypatch, [b"\x1b[I"])

    assert _reader()._read_key(0.2) == "ignore"


def test_playback_applies_output_and_ignores_input() -> None:
    recording = _FakeRecording(
        [
            Frame(1, 0.0, "o", b"hello\r\n"),
            Frame(2, 0.1, "i", b"ls\n"),
            Frame(3, 0.2, "o", b"world"),
        ]
    )
    playback = Playback(recording, screen_factory=_screen_factory)  # type: ignore[arg-type]
    playback.seek(recording.duration())

    assert playback.screen.text() == "hello\nworld"


def test_playback_seek_only_applies_frames_up_to_the_target() -> None:
    recording = _FakeRecording(
        [
            Frame(1, 0.0, "o", b"a"),
            Frame(2, 1.0, "o", b"b"),
            Frame(3, 2.0, "o", b"c"),
        ]
    )
    playback = Playback(recording, screen_factory=_screen_factory)  # type: ignore[arg-type]

    playback.seek(1.5)
    assert playback.screen.text() == "ab"
    playback.seek(0.5)
    assert playback.screen.text() == "a"
    assert playback.position == 0.5


def test_seek_frame_stops_at_the_frame_even_with_equal_timestamps() -> None:
    recording = _FakeRecording(
        [
            Frame(1, 0.0, "o", b"a"),
            Frame(2, 0.0, "o", b"b"),
            Frame(3, 0.0, "o", b"c"),
        ]
    )
    playback = Playback(recording, screen_factory=_screen_factory)  # type: ignore[arg-type]

    playback.seek_frame(1)
    assert playback.screen.text() == "a"
    playback.seek_frame(2)
    assert playback.screen.text() == "ab"


def test_seek_tolerates_microsecond_rounding_at_the_target() -> None:
    recording = _FakeRecording([Frame(1, 1.0000004, "o", b"a")])
    playback = Playback(recording, screen_factory=_screen_factory)  # type: ignore[arg-type]

    playback.seek(1.0)

    assert playback.screen.text() == "a"


def test_resize_frame_changes_the_screen_geometry() -> None:
    recording = _FakeRecording(
        [
            Frame(1, 0.0, "r", b"10x3"),
            Frame(2, 0.1, "o", b"x"),
        ]
    )
    playback = Playback(recording, screen_factory=_screen_factory)  # type: ignore[arg-type]
    playback.seek(1.0)

    assert (playback.screen.rows, playback.screen.columns) == (3, 10)
    assert "x" in playback.screen.text()


def test_repeated_arrow_keys_accumulate_into_one_seek(monkeypatch) -> None:
    from lls.recording.replay import tui

    player = object.__new__(tui._Player)
    player._pending_seek = None
    player._paused = False
    player._duration = 100.0
    player._origin_at = 10.0
    player._origin_wall = 0.0
    player._speed = 1.0
    monkeypatch.setattr(tui.time, "monotonic", lambda: 0.0)
    seeks: list[float] = []
    player._seek = seeks.append  # type: ignore[method-assign]

    player._seek_relative(1)
    player._seek_relative(1)
    player._seek_relative(1)

    assert player._pending_seek == 13.0
    player._apply_pending_seek()
    assert seeks == [13.0]
    assert player._pending_seek is None


def test_tui_seek_replays_forward_without_rebuilding() -> None:
    import io
    from types import SimpleNamespace

    from lls.recording.replay.tui import _Player

    feeds: list[bytes] = []

    class _Spy(PyteScreenModel):
        def feed(self, data: bytes) -> None:
            feeds.append(data)
            super().feed(data)

    def factory(rows: int, columns: int) -> _Spy:
        return _Spy(rows=rows, columns=columns)

    class _Recording:
        meta = SimpleNamespace(id="r1")
        geometry = (5, 20)

        def __init__(self) -> None:
            self._frames = [
                Frame(1, 0.0, "o", b"a"),
                Frame(2, 1.0, "o", b"b"),
                Frame(3, 2.0, "o", b"c"),
            ]

        def frames(self, **kwargs):
            return iter(self._frames)

        def duration(self) -> float:
            return 2.0

    class _Terminal:
        out = io.BytesIO()

    player = _Player(
        _Terminal(),  # type: ignore[arg-type]
        _Recording(),  # type: ignore[arg-type]
        size=(5, 20),
        start_at=0.0,
        start_frame=None,
        speed=1.0,
        show_input=False,
        screen_factory=factory,
        row_renderer=None,
    )
    assert feeds == [b"a"]

    player._seek(2.0)
    # 向前定位从当前位置续放，不 reset，也不重喂已经放过的 "a"。
    assert feeds == [b"a", b"b", b"c"]
    assert player._playback.screen.text() == "abc"

    player._seek(0.5)
    # 往回才重建：新屏从第一帧喂到 0.5s。
    assert feeds[-1] == b"a"
    assert player._playback.screen.text() == "a"
    assert player._playback.position == pytest.approx(0.5)


def test_painter_draws_the_model_without_replaying_host_sequences() -> None:
    screen = PyteScreenModel(rows=4, columns=20)
    screen.feed(b"\x1b]0;owned\x07\x1b]52;c;c2VjcmV0\x07hello")
    out = io.BytesIO()
    painter = ScreenPainter(out, viewport=lambda: (4, 20))

    painter.paint(screen, full=True)

    rendered = out.getvalue()
    assert b"hello" in rendered
    assert b"\x1b]52" not in rendered
    assert b"\x1b]0;" not in rendered


def test_painter_can_use_the_lls_row_renderer() -> None:
    from lls.replay_adapter import row_renderer

    screen = PyteScreenModel(rows=2, columns=20)
    screen.feed(b"\x1b[31mred\x1b[0m")
    out = io.BytesIO()
    painter = ScreenPainter(out, row_renderer=row_renderer, viewport=lambda: (2, 20))

    painter.paint(screen, full=True)

    assert b"red" in out.getvalue()


def test_painter_clips_to_the_viewport() -> None:
    screen = PyteScreenModel(rows=4, columns=20)
    screen.feed(b"0123456789abcdefghij")
    out = io.BytesIO()
    painter = ScreenPainter(out, viewport=lambda: (4, 10))

    painter.paint(screen, full=True)

    rendered = out.getvalue()
    assert b"0123456789" in rendered
    assert b"abcdefghij" not in rendered


def _record(
    tmp_path: Path, *, rows: int | None = 24, columns: int | None = 80
) -> TerminalRecorder:
    recorder = TerminalRecorder(
        tmp_path,
        recording_id="r1",
        rows=rows,
        columns=columns,
        compress=False,
        fsync=False,
    )
    return recorder


def test_export_asciicast_handles_split_utf8_and_absolute_time(tmp_path) -> None:
    recorder = _record(tmp_path)
    text = "你好".encode()
    recorder.record("o", b"hello\n")
    recorder.record("o", text[:2])
    recorder.record("o", text[2:])
    recorder.record("r", b"100x40")
    recorder.close()

    recording = Recording.open(tmp_path / "r1")
    out = io.StringIO()
    export_asciicast(recording, out)

    lines = out.getvalue().splitlines()
    header = json.loads(lines[0])
    assert header["version"] == 2
    assert header["width"] == 80
    assert header["height"] == 24
    assert header["duration"] == pytest.approx(recording.duration())

    events = [json.loads(line) for line in lines[1:]]
    output = "".join(event[2] for event in events if event[1] == "o")
    assert output == "hello\n你好"
    assert any(event[1] == "r" and event[2] == "100x40" for event in events)
    assert all(event[0] >= 0 for event in events)


def test_export_can_include_input(tmp_path) -> None:
    recorder = _record(tmp_path)
    recorder.record("i", b"secret\n")
    recorder.record("o", b"ok\n")
    recorder.close()
    recording = Recording.open(tmp_path / "r1")

    without = io.StringIO()
    export_asciicast(recording, without)
    assert not any(json.loads(line)[1] == "i" for line in without.getvalue().splitlines()[1:])

    out_with_input = io.StringIO()
    export_asciicast(recording, out_with_input, with_input=True)
    assert any(
        json.loads(line)[1] == "i"
        for line in out_with_input.getvalue().splitlines()[1:]
    )


def test_export_requires_a_geometry(tmp_path) -> None:
    recorder = _record(tmp_path, rows=None, columns=None)
    recorder.record("o", b"ok\n")
    recorder.close()
    recording = Recording.open(tmp_path / "r1")

    with pytest.raises(RecordingError, match="尺寸"):
        export_asciicast(recording, io.StringIO())
