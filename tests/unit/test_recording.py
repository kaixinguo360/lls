"""终端录制与 bash 工具引用。"""

from __future__ import annotations

import asyncio
import json

from lls.ai.tools.shell import Shell, bash
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


def test_bash_records_and_returns_ref(tmp_path) -> None:
    recorder = TerminalRecorder(tmp_path, recording_id="r2")
    shell = Shell(str(tmp_path), recorder=recorder)
    result = asyncio.run(bash({"command": "printf hi", "cwd": "", "timeout": 10}, shell))
    recorder.close()

    assert result.content.startswith("（退出码 0")
    assert result.meta["terminal_ref"].startswith("r2:")
    assert shell.last_ref is not None
    assert events_have(recorder, "i", b"printf hi\n")
    assert events_have(recorder, "o", b"hi")


def events_have(recorder: TerminalRecorder, channel: str, data: bytes) -> bool:
    return any(
        event["channel"] == channel and data in event["data"]
        for event in recorder.iter_events()
    )
