"""离线导出：只读 v2 事件，只留成功轮次，求助痕迹不进 SFT。"""

from __future__ import annotations

import json
from pathlib import Path

from lls.ai import ToolCall
from lls.ai.sessions import SessionStore
from lls.learn import cli
from lls.learn.dataset import build_samples, export


def _session(tmp_path: Path):
    keeper = SessionStore(root=tmp_path)
    session = keeper.create("stub", "m")
    session.append_system("SYSTEM", turn=1)
    session.append_request_header(
        {"config": {"provider": "stub", "model": "m"}, "tools": [{"name": "bash"}]},
        "r1",
    )

    session.append_turn_start(1)
    session.append_user("列文件", turn=1)
    call = ToolCall("c1", "bash", "{}")
    session.append_assistant("", (call,), turn=1, step=1)
    session.append_tool_result(call, "a.txt", turn=1, step=1)
    session.append_assistant("完成", turn=1, step=1)
    session.append_turn_end("ok", turn=1)

    session.append_turn_start(2)
    session.append_user("会失败", turn=2)
    session.append_assistant("坏了", turn=2, step=1)
    session.append_turn_end("error", turn=2)

    session.append_turn_start(3)
    session.append_user("卡住了", turn=3)
    ask = ToolCall("t1", "ask_teacher", '{"question":"怎么办"}')
    session.append_assistant("", (ask,), turn=3, step=1)
    session.append_tool_result(ask, "先看 stderr", turn=3, step=1)
    session.append_assistant("我先看 stderr", turn=3, step=1)
    session.append_turn_end("ok", turn=3)
    return keeper, session


def test_export_keeps_successful_turns_and_strips_teacher(tmp_path: Path) -> None:
    _keeper, session = _session(tmp_path)
    out = tmp_path / "out"

    manifest = export(tmp_path, out)

    assert manifest["samples"] == 2
    assert manifest["sessions"] == 1
    rows = [
        json.loads(line)
        for line in (out / "sft.jsonl").read_text("utf-8").splitlines()
    ]
    assert [row["meta"]["turn"] for row in rows] == [1, 3]
    assert rows[0]["messages"][0] == {"role": "system", "content": "SYSTEM"}
    assert rows[0]["tools"] == [{"name": "bash"}]
    assert any(
        call["function"]["name"] == "bash"
        for message in rows[0]["messages"]
        for call in message.get("tool_calls", [])
    )
    teacher = rows[1]
    assert all(
        call["function"]["name"] != "ask_teacher"
        for message in teacher["messages"]
        for call in message.get("tool_calls", [])
    )
    assert all(m["role"] != "tool" for m in teacher["messages"])
    assert teacher["messages"][-1]["content"] == "我先看 stderr"
    session.close()


def test_failed_turns_can_be_included_explicitly(tmp_path: Path) -> None:
    _keeper, session = _session(tmp_path)
    samples = list(build_samples(tmp_path, include_failed=True))
    assert [sample["meta"]["turn"] for sample in samples] == [1, 2, 3]
    session.close()


def test_cli_export_writes_manifest(tmp_path: Path, capsys) -> None:
    _keeper, session = _session(tmp_path)
    out = tmp_path / "cli-out"

    code = cli.main(["--sessions", str(tmp_path), "export", "--out", str(out)])

    assert code == 0
    manifest = json.loads((out / "manifest.json").read_text("utf-8"))
    assert manifest["samples"] == 2
    assert "wrote:" in capsys.readouterr().out
    session.close()
