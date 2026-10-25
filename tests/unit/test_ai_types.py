"""AI 类型与工具调用归并的单元测试。

覆盖两件事：请求体的形状要对得上 OpenAI 协议；流式分片的工具调用必须按
``index`` 归并成完整调用（分片边界由后端决定，不能假设「一次给全」）。
"""

from __future__ import annotations

from types import SimpleNamespace

from lls.ai import Chunk, ChunkKind, Message, ProviderConfig, Tool, ToolCall
from lls.ai.providers._wire import merge_tool_fragments
from lls.ai.providers.openai import OpenAIProvider

# ---- 请求体 ----


def _provider(**kwargs) -> OpenAIProvider:
    config = ProviderConfig(id="p", type="openai", api_key="k", **kwargs)
    return OpenAIProvider(config)


def _body(provider: OpenAIProvider):
    return provider._request_body(
        "gpt-4o-mini",
        [Message("user", "hi").to_payload()],
        [Tool(name="emit_input", parameters={"type": "object"})],
        False,
        {},
    )


def test_request_body_streams_and_carries_tools() -> None:
    body = _body(_provider())

    assert body["stream"] is True
    assert body["model"] == "gpt-4o-mini"
    assert body["tools"][0]["function"]["name"] == "emit_input"
    assert body["parallel_tool_calls"] is False


def test_params_are_passed_through() -> None:
    body = _body(_provider(params={"temperature": 0.1}))

    assert body["temperature"] == 0.1


def test_parallel_tool_calls_is_omitted_when_unset() -> None:
    body = _provider()._request_body(
        "m", [Message("user", "hi").to_payload()], None, None, {}
    )

    assert "parallel_tool_calls" not in body
    assert "tools" not in body


# ---- Tool ----


def test_tool_payload_shape() -> None:
    tool = Tool(
        name="emit_input",
        description="return text",
        parameters={"type": "object", "properties": {}},
        strict=True,
    )

    assert tool.to_payload() == {
        "type": "function",
        "function": {
            "name": "emit_input",
            "description": "return text",
            "parameters": {"type": "object", "properties": {}},
            "strict": True,
        },
    }


def test_tool_omits_empty_optional_fields() -> None:
    payload = Tool(name="f", parameters={"type": "object"}).to_payload()

    assert "description" not in payload["function"]
    assert "strict" not in payload["function"]


# ---- Message ----


def test_message_plain_payload() -> None:
    assert Message("user", "hi").to_payload() == {"role": "user", "content": "hi"}


def test_message_with_tool_calls() -> None:
    message = Message(
        "assistant",
        None,
        tool_calls=(ToolCall(id="call_1", name="emit_input", arguments='{"a":1}'),),
    )

    assert message.to_payload() == {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "emit_input", "arguments": '{"a":1}'},
            }
        ],
    }


def test_tool_message_carries_call_id() -> None:
    payload = Message("tool", "ok", tool_call_id="call_1").to_payload()

    assert payload["tool_call_id"] == "call_1"


def test_plain_message_has_no_tool_keys() -> None:
    payload = Message("user", "hi").to_payload()

    assert "tool_calls" not in payload
    assert "tool_call_id" not in payload


# ---- Chunk ----


def test_chunk_defaults() -> None:
    chunk = Chunk(ChunkKind.TEXT, "abc")

    assert chunk.text == "abc"
    assert chunk.call is None


# ---- 工具调用分片归并 ----


def _fragment(index: int, *, id=None, name=None, arguments=None):
    return SimpleNamespace(
        index=index,
        id=id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _merge(fragments):
    pending: dict = {}
    merge_tool_fragments(pending, fragments)
    return pending


def test_merge_concatenates_argument_fragments() -> None:
    pending = _merge(
        [
            _fragment(0, id="call_1", name="emit_input", arguments=""),
            _fragment(0, arguments='{"inp'),
            _fragment(0, arguments='ut": "ls -l"}'),
        ]
    )

    assert pending[0]["id"] == "call_1"
    assert pending[0]["name"] == "emit_input"
    assert pending[0]["arguments"] == '{"input": "ls -l"}'


def test_merge_keeps_parallel_calls_separate() -> None:
    pending = _merge(
        [
            _fragment(0, id="a", name="first", arguments="{"),
            _fragment(1, id="b", name="second", arguments="{"),
            _fragment(0, arguments="}"),
            _fragment(1, arguments="}"),
        ]
    )

    assert pending[0]["id"] == "a" and pending[0]["arguments"] == "{}"
    assert pending[1]["id"] == "b" and pending[1]["arguments"] == "{}"


def test_merge_ignores_fragments_without_function() -> None:
    pending = _merge([SimpleNamespace(index=2, id="x", function=None)])

    assert pending[2]["name"] == ""


def test_merge_tolerates_missing_index() -> None:
    pending = _merge([SimpleNamespace(id="x", function=SimpleNamespace(name="f", arguments="{}"))])

    assert pending[0]["name"] == "f"
