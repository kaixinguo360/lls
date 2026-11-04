"""终端录制、压缩、滚动与事件引用。"""

from __future__ import annotations

import asyncio
import json

from lls.ai.sessions import SessionStore
from lls.recording import TerminalRecorder, TerminalRef, parse_ref
from lls.session.engine import Engine
from lls.term import PyteScreenModel


def test_recording_roundtrip(tmp_path) -> None:
    recorder = TerminalRecorder(tmp_path, recording_id="r1", compress=False)
    first = recorder.record("i", "echo hi\n")
    second = recorder.record("o", b"hi\n")
    recorder.close()

    assert first == TerminalRef("r1", 1)
    assert str(second) == "r1:2"
    assert parse_ref(str(second)) == second

    events = list(recorder.iter_events())
    assert [event["channel"] for event in events] == ["i", "o"]
    assert events[0]["data"] == b"echo hi\n"
    assert events[1]["data"] == b"hi\n"

    index = (tmp_path / "r1" / "index.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(index[1])["frame"] == 2


def test_compression_removes_plain_stream(tmp_path) -> None:
    recorder = TerminalRecorder(tmp_path, recording_id="r1", compress=True)
    recorder.record("o", b"hello\n")
    recorder.close()
    assert not (tmp_path / "r1" / "segment-000001.cast").exists()
    assert (tmp_path / "r1" / "segment-000001.cast.gz").is_file()
    assert list(recorder.iter_events())[0]["data"] == b"hello\n"


def test_recording_rolls_at_single_file_limit(tmp_path) -> None:
    recorder = TerminalRecorder(
        tmp_path,
        recording_id="r1",
        compress=False,
        max_recording_bytes=1024,
        max_total_bytes=0,
    )
    recorder.record("o", b"x" * 3000)
    assert recorder.recording_id == "r1"
    segments = list((tmp_path / "r1").glob("segment-*.cast"))
    assert 1 <= len(segments) < 6
    assert sum(path.stat().st_size for path in segments) <= 2048
    recorder.close()


def test_total_limit_prunes_old_recordings(tmp_path) -> None:
    first = TerminalRecorder(
        tmp_path,
        recording_id="old",
        compress=False,
        max_recording_bytes=10_000,
        max_total_bytes=1,
    )
    first.record("o", b"x" * 100)
    first.close()
    second = TerminalRecorder(
        tmp_path,
        recording_id="new",
        compress=False,
        max_recording_bytes=10_000,
        max_total_bytes=1,
    )
    second.record("o", b"x" * 100)
    second.close()
    assert not (tmp_path / "old").exists()
    assert (tmp_path / "new").is_dir()


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
    recorder = TerminalRecorder(tmp_path, recording_id="r1", compress=False, fsync=False)
    recorder.record("o", b"ok\n")
    recorder.close()
    cast = tmp_path / "r1" / "segment-000001.cast"
    cast.write_text(cast.read_text(encoding="utf-8") + '{"broken"\n', encoding="utf-8")
    assert [event["data"] for event in recorder.iter_events()] == [b"ok\n"]


def test_recover_keeps_valid_gzip_and_drops_plain(tmp_path) -> None:
    recorder = TerminalRecorder(tmp_path, recording_id="r1", compress=False, fsync=False)
    recorder.record("o", b"ok\n")
    recorder.close()
    plain = tmp_path / "r1" / "segment-000009.cast"
    plain.write_text('{"version": 1}\n', encoding="utf-8")
    packed = plain.with_suffix(".cast.gz")
    import gzip
    with gzip.open(packed, "wt", encoding="utf-8") as handle:
        handle.write(plain.read_text(encoding="utf-8"))
    TerminalRecorder(tmp_path, recording_id="r1", compress=True, fsync=False)
    assert packed.is_file()
    assert not plain.exists()


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
