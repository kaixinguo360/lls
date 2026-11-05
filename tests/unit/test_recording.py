"""终端录制格式、读取端与写端降级。"""

from __future__ import annotations

import asyncio
import gzip
import json
import os
import stat
from pathlib import Path

import pytest

from lls.ai.sessions import SessionStore
from lls.app import _terminal_size
from lls.recording import (
    FrameUnavailable,
    Recording,
    RecordingCorrupt,
    TerminalRecorder,
    TerminalRef,
    parse_ref,
    tombstone_time,
)
from lls.session.engine import Engine
from lls.term import PyteScreenModel


def _recorder(tmp_path: Path, **kwargs) -> TerminalRecorder:
    return TerminalRecorder(
        tmp_path,
        recording_id=kwargs.pop("recording_id", "r1"),
        rows=kwargs.pop("rows", 24),
        columns=kwargs.pop("columns", 80),
        compress=kwargs.pop("compress", False),
        fsync=kwargs.pop("fsync", False),
        **kwargs,
    )


def test_terminal_size_falls_back_when_the_pty_reports_zero(monkeypatch) -> None:
    monkeypatch.setattr(
        "lls.app.os.get_terminal_size", lambda: os.terminal_size((0, 0))
    )

    assert _terminal_size() == os.terminal_size((80, 24))


def test_zero_terminal_size_is_treated_as_unknown(tmp_path) -> None:
    recorder = TerminalRecorder(
        tmp_path,
        recording_id="r1",
        rows=0,
        columns=0,
        compress=False,
        fsync=False,
    )
    recorder.record("o", b"x")
    recorder.close()

    assert Recording.open(tmp_path / "r1").geometry is None


def test_recording_id_cannot_escape_the_root(tmp_path) -> None:
    with pytest.raises(ValueError):
        TerminalRecorder(tmp_path, recording_id="../outside", rows=24, columns=80)
    with pytest.raises(ValueError):
        TerminalRecorder(tmp_path, recording_id="", rows=24, columns=80)


def test_recording_roundtrip(tmp_path) -> None:
    recorder = _recorder(tmp_path)
    first = recorder.record("i", "echo hi\n")
    second = recorder.record("o", b"hi\n")
    third = recorder.record("r", b"100x40")
    recorder.close()

    assert first == TerminalRef("r1", 1)
    assert str(second) == "r1:2"
    assert parse_ref(str(third)) == third

    recording = Recording.open(tmp_path / "r1")
    frames = list(recording.frames())
    assert [frame.number for frame in frames] == [1, 2, 3]
    assert [frame.channel for frame in frames] == ["i", "o", "r"]
    assert frames[0].data == b"echo hi\n"
    assert frames[1].data == b"hi\n"
    assert frames[0].at <= frames[1].at <= frames[2].at

    assert recording.meta.closed_at is not None
    assert recording.meta.frames == 3
    assert recording.geometry == (24, 80)
    assert recording.geometry_at(frames[2].at) == (40, 100)
    assert recording.frame_time(3) == pytest.approx(frames[2].at)
    assert recording.duration() == pytest.approx(frames[2].at)

    index = (tmp_path / "r1" / "index.jsonl").read_text(encoding="utf-8").splitlines()
    last = json.loads(index[-1])
    assert last["frame"] == 3
    assert last["at"] == pytest.approx(frames[2].at)


def test_compression_removes_plain_stream(tmp_path) -> None:
    recorder = _recorder(tmp_path, compress=True)
    recorder.record("o", b"hello\n")
    recorder.close()
    assert not (tmp_path / "r1" / "segment-000001.cast").exists()
    assert (tmp_path / "r1" / "segment-000001.cast.gz").is_file()
    assert list(Recording.open(tmp_path / "r1").frames())[0].data == b"hello\n"


def _header(path: Path) -> dict:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as handle:
        return json.loads(handle.readline())


def test_recording_rolls_at_single_file_limit(tmp_path) -> None:
    recorder = _recorder(
        tmp_path,
        compress=True,
        max_recording_bytes=1024,
        max_total_bytes=0,
    )
    for _ in range(12):
        recorder.record("o", b"x" * 300)
    recorder.close()

    recording = Recording.open(tmp_path / "r1")
    frames = list(recording.frames())
    assert frames
    assert frames[0].number == recording.meta.retained_from
    assert [frame.number for frame in frames] == list(
        range(frames[0].number, frames[0].number + len(frames))
    )
    assert all(
        left.at <= right.at for left, right in zip(frames, frames[1:])
    )
    segments = sorted((tmp_path / "r1").glob("segment-*.cast*"))
    assert len(segments) >= 2
    headers = [_header(path) for path in segments]
    assert [header["first_frame"] for header in headers] == sorted(
        header["first_frame"] for header in headers
    )
    assert all(header["version"] == 2 for header in headers)
    assert all("time_base" in header for header in headers)


def test_resize_before_rotation_is_written_to_the_next_segment_header(tmp_path) -> None:
    recorder = _recorder(
        tmp_path, compress=True, max_recording_bytes=1024, max_total_bytes=0
    )
    recorder.record("o", b"a" * 500)
    recorder.record("r", b"100x40")
    recorder.record("o", b"b" * 600)
    recorder.close()

    segments = sorted((tmp_path / "r1").glob("segment-*.cast*"))
    headers = [_header(path) for path in segments]
    assert headers[0]["rows"] == 24
    assert headers[0]["columns"] == 80
    assert headers[-1]["rows"] == 40
    assert headers[-1]["columns"] == 100


def test_engine_records_resize_frames(tmp_path) -> None:
    screen = PyteScreenModel(rows=5, columns=20)
    recorder = _recorder(tmp_path, rows=5, columns=20)

    async def scenario():
        engine = Engine(None, [], screen, {}, "default", recorder=recorder)
        engine._exit_event = asyncio.Event()
        engine.resize(3, 10)
        return engine

    asyncio.run(scenario())
    recorder.close()

    frames = list(Recording.open(tmp_path / "r1").frames())
    assert [frame.channel for frame in frames] == ["r"]
    assert frames[0].data == b"10x3"


def test_total_limit_prunes_old_recordings(tmp_path) -> None:
    first = _recorder(
        tmp_path,
        recording_id="old",
        max_recording_bytes=10_000,
        max_total_bytes=1,
    )
    first.record("o", b"x" * 100)
    first.close()
    second = _recorder(
        tmp_path,
        recording_id="new",
        max_recording_bytes=10_000,
        max_total_bytes=1,
    )
    second.record("o", b"x" * 100)
    second.close()

    assert not (tmp_path / "old").exists()
    assert (tmp_path / "new").is_dir()
    deleted = tombstone_time(tmp_path, "old")
    assert deleted is not None


def test_every_event_carries_current_terminal_cursor(tmp_path) -> None:
    store = SessionStore(root=tmp_path)
    store.set_terminal(lambda: TerminalRef("r", 7))
    session = store.create("p", "m")
    session.append_user("hello")
    refs = [
        event.data["lls"]["terminal_cursor"]
        for event in session.events()
        if isinstance(event.data.get("lls"), dict)
        and "terminal_cursor" in event.data["lls"]
    ]
    assert refs
    assert refs[0] == {"recording_id": "r", "frame": 7}
    session.close()
    store.set_terminal(None)


def test_the_cursor_stops_when_the_source_goes_quiet(tmp_path) -> None:
    """录制停用后来源返回 None：事件宁可不带游标，也不带一个早就不动的帧号。"""
    store = SessionStore(root=tmp_path)
    alive = {"value": True}
    store.set_terminal(lambda: TerminalRef("r", 3) if alive["value"] else None)
    session = store.create("p", "m")

    session.append_user("录着")
    alive["value"] = False
    session.append_user("停了")

    users = [event for event in session.events() if event.type == "user/message"]
    assert users[0].data["lls"]["terminal_cursor"] == {"recording_id": "r", "frame": 3}
    assert "terminal_cursor" not in users[1].data.get("lls", {})
    session.close()
    store.set_terminal(None)


def test_truncated_tail_is_ignored(tmp_path) -> None:
    recorder = _recorder(tmp_path)
    recorder.record("o", b"ok\n")
    recorder.close()
    cast = tmp_path / "r1" / "segment-000001.cast"
    cast.write_text(cast.read_text(encoding="utf-8") + '{"broken"\n', encoding="utf-8")
    recording = Recording.open(tmp_path / "r1")
    assert [frame.data for frame in recording.frames()] == [b"ok\n"]
    assert any("损坏" in warning for warning in recording.warnings)


def test_recover_keeps_valid_gzip_and_drops_plain(tmp_path) -> None:
    recorder = _recorder(tmp_path)
    recorder.record("o", b"ok\n")
    recorder.close()

    plain = tmp_path / "r1" / "segment-000009.cast"
    plain.write_text('{"version": 2}\n', encoding="utf-8")
    packed = plain.with_suffix(".cast.gz")
    with gzip.open(packed, "wt", encoding="utf-8") as handle:
        handle.write(plain.read_text(encoding="utf-8"))

    TerminalRecorder(
        tmp_path,
        recording_id="r1",
        rows=24,
        columns=80,
        compress=True,
        fsync=False,
    ).close()
    assert packed.is_file()
    assert not plain.exists()


def test_open_rejects_unknown_format_version(tmp_path) -> None:
    recorder = _recorder(tmp_path)
    recorder.record("o", b"ok\n")
    recorder.close()
    meta_path = tmp_path / "r1" / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["version"] = 1
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    with pytest.raises(RecordingCorrupt, match="不支持的录制版本"):
        Recording.open(tmp_path / "r1")


def test_missing_segment_is_reported_and_later_frames_still_play(tmp_path) -> None:
    recorder = _recorder(
        tmp_path, compress=True, max_recording_bytes=1024, max_total_bytes=0
    )
    for _ in range(8):
        recorder.record("o", b"x" * 300)
    recorder.close()

    segments = sorted((tmp_path / "r1").glob("segment-*.cast*"))
    assert len(segments) >= 2
    segments[0].unlink()
    recording = Recording.open(tmp_path / "r1")
    frames = list(recording.frames())
    assert frames
    assert any("缺少分段" in warning for warning in recording.warnings)


def test_frame_outside_the_retained_window_is_unavailable(tmp_path) -> None:
    recorder = _recorder(tmp_path, max_recording_bytes=1024, max_total_bytes=0)
    for _ in range(12):
        recorder.record("o", b"x" * 300)
    recorder.close()

    recording = Recording.open(tmp_path / "r1")
    assert recording.meta.retained_from > 1
    with pytest.raises(FrameUnavailable):
        recording.frame(1)


@pytest.mark.skipif(os.name != "posix", reason="POSIX 权限位")
def test_recording_files_are_private(tmp_path) -> None:
    recorder = _recorder(tmp_path)
    recorder.record("o", b"ok\n")
    recorder.close()

    root = tmp_path / "r1"
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    for name in ("meta.json", "index.jsonl", "segment-000001.cast"):
        assert stat.S_IMODE((root / name).stat().st_mode) == 0o600


# ---- 录制是旁路：它坏了只停用自己 ----


class _Boom:
    """写一帧就炸的录制器：模拟磁盘满。"""

    def __init__(self) -> None:
        self.attempts = 0

    def record(self, channel: str, data: object) -> object:
        self.attempts += 1
        raise OSError(28, "No space left on device")


def test_a_failing_recorder_does_not_break_the_output_path() -> None:
    """磁盘满不该让终端读不出东西：停用录制，画面照常喂给屏幕模型。"""
    screen = PyteScreenModel(rows=4, columns=20)
    recorder = _Boom()

    async def scenario():
        engine = Engine(None, [], screen, {}, "default", recorder=recorder)  # type: ignore[arg-type]
        engine._exit_event = asyncio.Event()
        engine._read_all_available = lambda: b"hello"
        engine._on_master_readable()
        engine._on_master_readable()
        return engine

    engine = asyncio.run(scenario())

    assert "hello" in screen.text()
    # 只试一次：停用之后每帧直接跳过，不反复失败。
    assert recorder.attempts == 1
    assert engine.recorder_error.startswith("录制已停用")


def test_a_failing_recorder_does_not_block_typing() -> None:
    """输入也一样：先写子进程，录制失败不影响这一笔已经送出去的字节。"""
    screen = PyteScreenModel(rows=4, columns=20)
    recorder = _Boom()

    class Pty:
        def __init__(self) -> None:
            self.written = []

        def write(self, data: bytes) -> None:
            self.written.append(data)

    async def scenario():
        engine = Engine(None, [], screen, {}, "default", recorder=recorder)  # type: ignore[arg-type]
        engine._exit_event = asyncio.Event()
        engine._pty = Pty()  # type: ignore[assignment]
        engine._pty_write(b"ls\n")
        return engine

    engine = asyncio.run(scenario())

    assert engine._pty.written == [b"ls\n"]
    assert recorder.attempts == 1
    assert engine.recorder_error.startswith("录制已停用")
