"""工具调用分片的归并：provider 只交碎片，使用方拼成完整调用。

分片边界由后端决定，不能假设「一次给全」：``id`` / ``name`` 常只在首片出现，
``arguments`` 是 JSON 文本的一段，单看某片不是合法 JSON。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from lls.ai import ToolCallDelta, ToolCallMerger
from lls.ai.toolcalls import delta_from


def _merge(deltas) -> ToolCallMerger:
    merger = ToolCallMerger()
    for delta in deltas:
        merger.add(delta)
    return merger


def _one(merger: ToolCallMerger):
    calls = merger.calls()
    assert len(calls) == 1
    return calls[0]


def test_argument_fragments_are_concatenated() -> None:
    merger = _merge(
        [
            ToolCallDelta(0, "call_1", "emit_input", ""),
            ToolCallDelta(0, "", "", '{"inp'),
            ToolCallDelta(0, "", "", 'ut": "ls -l"}'),
        ]
    )

    call = _one(merger)
    assert call.id == "call_1"
    assert call.name == "emit_input"
    assert json.loads(call.arguments) == {"input": "ls -l"}


def test_parallel_calls_keep_separate_slots() -> None:
    merger = _merge(
        [
            ToolCallDelta(0, "a", "first", "{"),
            ToolCallDelta(1, "b", "second", "{"),
            ToolCallDelta(0, "", "", "}"),
            ToolCallDelta(1, "", "", "}"),
        ]
    )

    calls = merger.calls()
    assert [(call.id, call.name, call.arguments) for call in calls] == [
        ("a", "first", "{}"),
        ("b", "second", "{}"),
    ]


def test_repeated_name_is_not_appended() -> None:
    """有的服务每片都带上完整 name：逐片拼接会拼成 send_textsend_text。"""
    merger = _merge(
        [
            ToolCallDelta(0, "call_1", "send_text", ""),
            ToolCallDelta(0, "", "send_text", '{"text"'),
            ToolCallDelta(0, "", "send_text", ': "ls"}'),
        ]
    )

    assert _one(merger).name == "send_text"


def test_name_split_across_fragments_is_joined() -> None:
    merger = _merge(
        [ToolCallDelta(0, "call_1", "send", ""), ToolCallDelta(0, "", "_text", "{}")]
    )

    assert _one(merger).name == "send_text"


def test_nameless_slot_is_dropped() -> None:
    merger = _merge([ToolCallDelta(0, "call_1", "", '{"a": 1}')])

    assert merger.calls() == []


def test_progress_reports_the_call_being_built() -> None:
    merger = _merge(
        [
            ToolCallDelta(0, "call_1", "send_text", '{"input": '),
            ToolCallDelta(0, "", "", '"l'),
        ]
    )

    assert merger.progress() == ("send_text", '{"input": "l')


def test_progress_is_empty_before_anything_arrives() -> None:
    assert ToolCallMerger().progress() == ("", "")


def test_delta_from_reads_dict_and_sdk_object() -> None:
    raw = {
        "index": 1,
        "id": "call_9",
        "function": {"name": "emit", "arguments": '{"a"'},
    }
    sdk = SimpleNamespace(
        index=1,
        id="call_9",
        function=SimpleNamespace(name="emit", arguments='{"a"'),
    )

    assert delta_from(raw) == delta_from(sdk) == ToolCallDelta(1, "call_9", "emit", '{"a"')


def test_delta_from_tolerates_missing_pieces() -> None:
    assert delta_from({"function": None}) == ToolCallDelta(0, "", "", "")
    assert delta_from(SimpleNamespace(index=2)) == ToolCallDelta(2, "", "", "")
