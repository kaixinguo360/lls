"""只用标准库的 provider：对着本地桩走真实 HTTP。

这一份刻意不 import ``openai``，所以在任何环境都能跑 —— 它守的正是
「没有 SDK 也能用」这条。
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from lls.ai import ChunkKind, Message, ProviderConfig, Tool
from lls.ai.providers.http import HttpProvider
from lls.ai.providers.openai import ProviderError

TOOL = Tool(
    name="emit_input",
    description="return text",
    parameters={"type": "object", "properties": {}, "required": []},
    strict=True,
)


def _frame(delta, finish=None) -> bytes:
    payload = {
        "id": "chatcmpl-stub",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "stub",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return b"data: " + json.dumps(payload).encode() + b"\n\n"


class _Stub(BaseHTTPRequestHandler):
    """默认按「连接关闭即结束」回一段带工具调用的 SSE。"""

    requests: list = []
    status: int = 200
    chunked: bool = False
    body_frames: list = []

    def log_message(self, *args):
        pass

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        type(self).requests.append(
            {"headers": dict(self.headers), "body": json.loads(raw or b"{}")}
        )

        if type(self).status != 200:
            payload = b'{"error": {"message": "nope"}}'
            self.send_response(type(self).status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        frames = type(self).body_frames or [
            _frame({"content": "he"}),
            _frame({"content": "llo"}),
            _frame(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_stub",
                            "type": "function",
                            "function": {"name": "emit_input", "arguments": ""},
                        }
                    ]
                }
            ),
            _frame({"tool_calls": [{"index": 0, "function": {"arguments": '{"inp'}}]}),
            _frame({"tool_calls": [{"index": 0, "function": {"arguments": 'ut": "ls"}'}}]}),
            _frame({}, finish="tool_calls"),
            b"data: [DONE]\n\n",
        ]

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        if type(self).chunked:
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for frame in frames:
                self.wfile.write(b"%x\r\n" % len(frame) + frame + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
        else:
            self.send_header("Connection", "close")
            self.end_headers()
            for frame in frames:
                self.wfile.write(frame)
                self.wfile.flush()


@pytest.fixture()
def stub():
    _Stub.requests = []
    _Stub.status = 200
    _Stub.chunked = False
    _Stub.body_frames = []
    server = HTTPServer(("127.0.0.1", 0), _Stub)
    threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        ).start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        server.server_close()


def _provider(port: int, **kwargs) -> HttpProvider:
    return HttpProvider(
        ProviderConfig(
            id="stub",
            type="openai-http",
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


# ---- 流式内容 ----


def test_text_chunks_are_forwarded(stub) -> None:
    chunks = _chat(_provider(stub))

    assert "".join(c.text for c in chunks if c.kind is ChunkKind.TEXT) == "hello"


def test_tool_call_fragments_are_merged(stub) -> None:
    chunks = _chat(_provider(stub))

    call = [c for c in chunks if c.kind is ChunkKind.TOOL_CALL][0].call
    assert call.id == "call_stub"
    assert json.loads(call.arguments) == {"input": "ls"}


def test_stream_ends_with_done(stub) -> None:
    chunks = _chat(_provider(stub))

    assert chunks[-1].kind is ChunkKind.DONE


def test_single_tool_call_is_emitted_once(stub) -> None:
    chunks = _chat(_provider(stub))

    assert len([c for c in chunks if c.kind is ChunkKind.TOOL_CALL]) == 1


# ---- 请求本身 ----


def test_request_line_and_headers(stub) -> None:
    _chat(_provider(stub))

    headers = {k.lower(): v for k, v in _Stub.requests[-1]["headers"].items()}
    assert headers["host"] == "127.0.0.1:%d" % stub
    assert headers["content-type"] == "application/json"
    assert headers["accept"] == "text/event-stream"
    assert headers["authorization"] == "Bearer sk-test"
    assert headers["connection"] == "close"


def test_request_body_matches_the_protocol(stub) -> None:
    _chat(_provider(stub, params={"temperature": 0.2}), parallel_tool_calls=False)

    body = _Stub.requests[-1]["body"]
    assert body["model"] == "gpt-4o-mini"
    assert body["stream"] is True
    assert body["temperature"] == 0.2
    assert body["parallel_tool_calls"] is False
    assert body["tools"][0]["function"]["name"] == "emit_input"
    assert body["messages"] == [{"role": "user", "content": "hi"}]


def test_extra_headers_are_sent(stub) -> None:
    _chat(_provider(stub, headers={"X-Org": "team"}))

    headers = {k.lower(): v for k, v in _Stub.requests[-1]["headers"].items()}
    assert headers["x-org"] == "team"


def test_endpoint_appends_chat_completions() -> None:
    provider = HttpProvider(
        ProviderConfig(
            id="p",
            type="openai-http",
            base_url="https://example.com/v1/",
            api_key="k",
        )
    )

    assert provider._endpoint() == "https://example.com/v1/chat/completions"


def test_default_base_url() -> None:
    provider = HttpProvider(ProviderConfig(id="p", type="openai-http", api_key="k"))

    assert provider._endpoint() == "https://api.openai.com/v1/chat/completions"


# ---- 传输方式与错误 ----


def test_chunked_transfer_is_decoded(stub) -> None:
    _Stub.chunked = True

    chunks = _chat(_provider(stub))

    assert "".join(c.text for c in chunks if c.kind is ChunkKind.TEXT) == "hello"
    assert chunks[-1].kind is ChunkKind.DONE


def test_http_error_carries_status_and_body(stub) -> None:
    _Stub.status = 400

    with pytest.raises(ProviderError) as caught:
        _chat(_provider(stub))

    assert "HTTP 400" in str(caught.value)
    assert "nope" in str(caught.value)


def test_connection_refused_is_reported() -> None:
    provider = HttpProvider(
        ProviderConfig(id="p", type="openai-http", base_url="http://127.0.0.1:9/v1")
    )

    with pytest.raises(ProviderError):
        _chat(provider)


def test_unreachable_host_reports_the_endpoint() -> None:
    provider = HttpProvider(
        ProviderConfig(
            id="p", type="openai-http", base_url="http://127.0.0.1:8/v1", timeout=2
        )
    )

    with pytest.raises(ProviderError) as caught:
        _chat(provider)

    assert "127.0.0.1:8" in str(caught.value)


def test_malformed_chunk_is_reported(stub) -> None:
    _Stub.body_frames = [b"data: {not json}\n\n"]

    with pytest.raises(ProviderError) as caught:
        _chat(_provider(stub))

    assert "malformed" in str(caught.value)
