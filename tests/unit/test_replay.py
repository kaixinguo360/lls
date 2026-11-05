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


def test_key_decoder_recognizes_arrows_and_keeps_the_rest() -> None:
    from lls.recording.replay.tui import _decode_key

    assert _decode_key(b"\x1b[A") == ("up", b"")
    assert _decode_key(b"\x1b[Dq") == ("left", b"q")
    assert _decode_key(b"q") == ("q", b"")
    assert _decode_key(b"\x1b") == ("escape", b"")
    assert _decode_key(b"\x1b[") == (None, b"\x1b[")


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
