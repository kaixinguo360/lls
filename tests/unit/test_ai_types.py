"""AI 共享类型的单元测试：请求体形状要对得上 OpenAI 协议。

工具调用分片的归并见 ``test_ai_toolcalls.py``。
"""

from __future__ import annotations

from lls.ai import Chunk, ChunkKind, Message, ProviderConfig, Tool, ToolCall
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
    assert chunk.fragment is None
