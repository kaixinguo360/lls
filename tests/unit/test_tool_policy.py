"""工具策略：两层合并、过滤与序列化。

这一层是「工具开关」的存储与合成，不涉及界面：界面只改这里的值。
"""

from __future__ import annotations

import asyncio

from lls.ai.sessions.store import SessionMeta
from lls.ai.tool_options import ToolOptions, parse_tool_options, tool_options_from_json
from lls.ai.tools import builtin_tools
from lls.ai.tools.policy import ToolPolicy, global_policy, reset_global_policy


class FakePort:
    screen = None

    def write(self, data: bytes, *, enter: bool = False) -> None:
        pass

    def flush_model(self) -> None:
        pass

    async def wait(self, timeout: float) -> None:
        await asyncio.sleep(0)


def _box():
    return builtin_tools(FakePort(), FakePort().wait)


# ---- 合并 ----


def test_field_wise_merge_prefers_the_nearer_layer() -> None:
    base = ToolOptions(enabled=False, confirm=True)
    near = ToolOptions(enabled=True)

    merged = base.merged_with(near)

    assert merged.enabled is True
    assert merged.confirm is True      # 近层没说，沿用远层
    assert merged.timeout is None      # 两层都没说


def test_a_tool_nobody_mentioned_stays_enabled() -> None:
    reset_global_policy()
    policy = global_policy().merged(ToolPolicy())

    assert policy.enabled("bash") is True


# ---- 过滤 ----


def test_disabled_tools_are_not_in_the_request_and_order_is_kept() -> None:
    reset_global_policy({"write_file": ToolOptions(enabled=False)})
    box = _box()
    kept = [entry.tool.name for entry in global_policy().filter(list(box))]

    assert "write_file" not in kept
    assert kept[0] == "read_terminal"     # 装配顺序不变
    assert len(kept) == len(list(box)) - 1
    reset_global_policy()


def test_session_layer_wins_over_global_for_the_same_field() -> None:
    reset_global_policy({"write_file": ToolOptions(enabled=False)})
    session = ToolPolicy.from_json({"write_file": {"enabled": True}})

    effective = global_policy().merged(session)

    assert effective.enabled("write_file") is True
    reset_global_policy()


def test_session_layer_can_only_affect_what_it_names() -> None:
    reset_global_policy({"write_file": ToolOptions(enabled=False)})
    session = ToolPolicy.from_json({"bash": {"enabled": False}})

    effective = global_policy().merged(session)

    assert effective.enabled("write_file") is False   # 全局仍生效
    assert effective.enabled("bash") is False
    assert effective.enabled("read_file") is True
    reset_global_policy()


# ---- 配置解析 ----


def test_bad_field_values_warn_and_are_ignored() -> None:
    notes: list[str] = []
    options = parse_tool_options({"enabled": "false", "confirm": True}, "ai.tools.x", notes)

    assert options.enabled is None
    assert options.confirm is True
    assert len(notes) == 1


def test_unknown_field_warns() -> None:
    notes: list[str] = []
    parse_tool_options({"bogus": 1}, "ai.tools.y", notes)

    assert any("bogus" in note for note in notes)


def test_meta_json_is_tolerant() -> None:
    policy = ToolPolicy.from_json({"bash": {"enabled": False}, "junk": "nope", 42: None})

    assert policy.disabled() == ["bash"]


# ---- 会话元信息 ----


def test_meta_roundtrip_keeps_tool_settings() -> None:
    meta = SessionMeta(id="x", tools={"write_file": {"enabled": False}})
    raw = meta.to_json()

    assert raw["tools"] == {"write_file": {"enabled": False}}
    assert tool_options_from_json(raw["tools"]["write_file"]).enabled is False


def test_meta_without_tools_reads_as_empty() -> None:
    meta = SessionMeta(id="x")

    assert meta.tools == {}
    assert ToolPolicy.from_json(meta.tools).disabled() == []
