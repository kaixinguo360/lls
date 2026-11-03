"""终端录制、压缩、滚动与事件引用。"""

from __future__ import annotations

import json

from lls.ai.sessions import SessionStore
from lls.recording import TerminalRecorder, TerminalRef, parse_ref


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
