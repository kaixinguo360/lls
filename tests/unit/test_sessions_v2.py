"""v2 事件契约：DSH 形状、parts、请求快照、工具指针与旧会话拒绝。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from lls.ai import Chunk, ChunkKind, Message, ToolCall
from lls.ai.sessions import SessionStore
from lls.ai.sessions.events import Event, decode, encode, fold
from lls.ai.sessions.store import LegacySession, SCHEMA_VERSION
from lls.learn.help import HintLedger, TranscriptView, build_help_tool
from lls.learn.teacher import ModelTeacher


def test_session_and_events_use_the_dsh_envelope(tmp_path: Path) -> None:
    keeper = SessionStore(root=tmp_path)
    session = keeper.create("stub", "m")
    session.append_turn_start(1)
    session.append_user("任务")
    session.append_turn_end("ok", turn=1)

    events = session.events()
    assert events[0].type == "session"
    assert events[0].data["version"] == SCHEMA_VERSION
    assert [event.type for event in events[1:]] == [
        "turn/start",
        "user/message",
        "turn/end",
    ]
    raw = json.loads((session.path / "events.jsonl").read_text("utf-8").splitlines()[0])
    assert raw["type"] == "session"
    assert "data" not in raw
    session.close()


def test_fold_reads_text_parts_and_tool_results(tmp_path: Path) -> None:
    keeper = SessionStore(root=tmp_path)
    session = keeper.create("stub", "m")
    session.append_user("任务")
    call = ToolCall("c1", "bash", "{}")
    session.append_assistant("先看看", (call,), reasoning="思考", turn=1, step=1)
    session.append_tool_result(call, "输出", turn=1, step=1)
    session.append_assistant("答复", turn=1, step=1)
    session.append_turn_end("ok", turn=1)

    assert session.history() == [
        Message("user", "任务"),
        Message("assistant", "先看看", tool_calls=(call,)),
        Message("tool", "输出", tool_call_id="c1"),
        Message("assistant", "答复"),
    ]
    session.close()


def test_tool_result_keeps_source_pointer_and_meta(tmp_path: Path) -> None:
    keeper = SessionStore(root=tmp_path)
    session = keeper.create("stub", "m")
    call = ToolCall("c1", "bash", "{}")
    event = session.append_tool_result(
        call, "输出", source_seq=7, meta={"lls": {"exit_code": 0}}
    )
    assert event is not None
    assert event.source_event_seqs == (7,)
    assert event.data["meta"]["lls"]["exit_code"] == 0
    session.close()


def test_big_tool_output_still_uses_blobs(tmp_path: Path) -> None:
    keeper = SessionStore(root=tmp_path)
    session = keeper.create("stub", "m")
    call = ToolCall("c1", "bash", "{}")
    session.append_assistant("", (call,), turn=1, step=1)
    session.append_tool_result(call, "大" * 5000, turn=1, step=1)
    event = session.events()[-1]
    part = event.data["message"]["content"][0]
    assert part["blob"]
    assert part["text"] == ""
    assert (session.path / "blobs" / part["blob"]).is_file()
    assert session.history()[-1] == Message("tool", "大" * 5000, tool_call_id="c1")
    session.close()


def test_request_snapshot_is_reconstructable(tmp_path: Path) -> None:
    keeper = SessionStore(root=tmp_path)
    session = keeper.create("stub", "m")
    session.append_system("系统", turn=1, render_id="r1")
    session.append_request_header(
        {"config": {"provider": "stub", "model": "m"}, "tools": []}, "r1"
    )
    session.append_request_context("stub", "m", 128000, 76800, "r1")
    events = session.events()
    assert [event.type for event in events[-3:]] == [
        "system/message",
        "request/header",
        "request/context",
    ]
    assert events[-1].data["lls"]["budget"] == 76800
    session.close()


def test_legacy_meta_is_not_listed_or_opened(tmp_path: Path) -> None:
    path = tmp_path / "20260101-0000-abcdef"
    path.mkdir()
    (path / "meta.json").write_text(
        json.dumps({"version": 1, "id": path.name}), encoding="utf-8"
    )
    (path / "events.jsonl").write_text("", encoding="utf-8")

    keeper = SessionStore(root=tmp_path)
    assert keeper.list() == []
    assert keeper._read_meta(path) is None
    with pytest.raises(LegacySession):
        from lls.ai.sessions.store import _meta_from_json

        _meta_from_json(json.loads((path / "meta.json").read_text("utf-8")), path.name)


def test_encode_decode_roundtrip() -> None:
    event = Event(
        type="tool/result",
        seq=4,
        time=123,
        data={"message": {"role": "tool", "content": []}},
        surface_op="append",
        source_event_seqs=(2,),
    )
    assert decode(json.loads(encode(event))) == event


class _StubProvider:
    id = "stub"

    def __init__(self, chunks):
        self.chunks = chunks
        self.tools = "unset"

    async def chat(self, model, messages, *, tools=None, **kwargs):
        self.tools = tools
        for chunk in self.chunks:
            yield chunk


def test_teacher_never_gets_tools_and_rejects_tool_calls() -> None:
    provider = _StubProvider(
        [Chunk(ChunkKind.TEXT, "先看 stderr"), Chunk(ChunkKind.DONE)]
    )
    teacher = ModelTeacher(provider, "m")
    reply = asyncio.run(teacher.advise("怎么办", [Message("user", "任务")]))
    assert reply.text == "先看 stderr"
    assert provider.tools is None

    bad = _StubProvider(
        [Chunk(ChunkKind.DONE)]
    )
    # 没有工具调用时返回空文本，应报“没有内容”，而不是把执行权带回来。
    with pytest.raises(RuntimeError):
        asyncio.run(ModelTeacher(bad, "m").advise("怎么办", []))


def test_help_budget_and_redaction() -> None:
    class _Teacher:
        async def advise(self, question, messages):
            assert "sk-secret" not in question
            return type("Reply", (), {"text": "看 stderr", "usage": {}})()

    ledger = HintLedger(max_per_turn=1, max_per_session=1, min_tool_calls=0)
    view = TranscriptView([Message("user", "任务")])
    tool = build_help_tool(_Teacher(), ledger, view, redact_patterns=[r"sk-[a-z]+"])
    text = asyncio.run(tool.run({"question": "sk-secret 怎么办"}))
    assert "导师建议" in text
    again = asyncio.run(tool.run({"question": "再问一次"}))
    assert again.startswith("error:")
