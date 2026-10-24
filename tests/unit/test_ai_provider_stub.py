"""provider 打真实 HTTP 的端到端测试（本地桩，不联网）。

用 stdlib 起一个最小的 OpenAI 兼容端点，验证真正握手的那一段：请求体的形状、
SSE 流式解析、以及工具调用分片按 ``index`` 归并。这些在纯单元测试里只能靠
假对象模拟，最容易与实际 SDK 行为脱节。
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

pytest.importorskip("openai")

from lls.ai import ChunkKind, Message, ProviderConfig, Tool  # noqa: E402
from lls.ai.providers.openai import OpenAIProvider  # noqa: E402

TOOL = Tool(
    name="emit_input",
    description="return text",
    parameters={"type": "object", "properties": {}, "required": []},
    strict=True,
)


class _Stub(BaseHTTPRequestHandler):
    """按 OpenAI 的 SSE 形状回一段工具调用，参数故意拆成三片。"""

    requests: list = []

    def log_message(self, *args):  # 安静
        pass

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        type(self).requests.append(body)

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()

        frames = [
            (
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_stub",
                            "type": "function",
                            "function": {"name": "emit_input", "arguments": ""},
                        }
                    ]
                },
                None,
            ),
            ({"tool_calls": [{"index": 0, "function": {"arguments": '{"input": '}}]}, None),
            ({"tool_calls": [{"index": 0, "function": {"arguments": '"ls -l"}'}}]}, None),
            ({}, "tool_calls"),
        ]
        for delta, finish in frames:
            frame = {
                "id": "chatcmpl-stub",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": body.get("model", "stub"),
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }
            self.wfile.write(b"data: " + json.dumps(frame).encode() + b"\n\n")
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


@pytest.fixture()
def stub():
    _Stub.requests = []
    server = HTTPServer(("127.0.0.1", 0), _Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        server.server_close()


def _provider(port: int, **kwargs) -> OpenAIProvider:
    return OpenAIProvider(
        ProviderConfig(
            id="stub",
            type="openai",
            base_url="http://127.0.0.1:%d/v1" % port,
            api_key="sk-test",
            **kwargs,
        )
    )


def _chat(provider, **kwargs):
    async def run():
        chunks = []
        async for chunk in provider.chat(
            "gpt-4o-mini", [Message("user", "hi")], tools=[TOOL], **kwargs
        ):
            chunks.append(chunk)
        return chunks

    return asyncio.run(run())


def test_streamed_tool_call_is_emitted(stub) -> None:
    chunks = _chat(_provider(stub), parallel_tool_calls=False)

    calls = [c for c in chunks if c.kind is ChunkKind.TOOL_CALL]
    assert len(calls) == 1
    assert calls[0].call.name == "emit_input"
    assert chunks[-1].kind is ChunkKind.DONE


def test_argument_fragments_are_merged(stub) -> None:
    chunks = _chat(_provider(stub), parallel_tool_calls=False)

    call = [c for c in chunks if c.kind is ChunkKind.TOOL_CALL][0].call
    assert call.id == "call_stub"
    assert call.arguments == '{"input": "ls -l"}'
    assert json.loads(call.arguments) == {"input": "ls -l"}


def test_request_body_matches_the_protocol(stub) -> None:
    _chat(_provider(stub, params={"temperature": 0.2}), parallel_tool_calls=False)

    body = _Stub.requests[-1]
    assert body["model"] == "gpt-4o-mini"
    assert body["stream"] is True
    assert body["temperature"] == 0.2
    assert body["parallel_tool_calls"] is False
    assert body["tools"][0]["function"]["name"] == "emit_input"
    assert body["tools"][0]["function"]["strict"] is True
    assert body["messages"] == [{"role": "user", "content": "hi"}]
