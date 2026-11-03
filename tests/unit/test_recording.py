"""终端录制与 bash 工具引用。"""

from __future__ import annotations

import asyncio
import json

from lls.ai.tools.shell import Shell, bash
from lls.ai.sessions import SessionStore
from lls.recording import TerminalRecorder, TerminalRef, parse_ref


def test_recording_roundtrip(tmp_path) -> None:
    recorder = TerminalRecorder(tmp_path, recording_id="r1")
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
