"""只用标准库的 OpenAI 兼容 provider。

和 :mod:`lls.ai.providers.openai` 说同一种协议，但不依赖 ``openai`` 包：
自己连 TCP、自己发 HTTP/1.1、自己解 SSE。用于不该装第三方 SDK 的场合。

一切都跑在事件循环里（``asyncio.open_connection``），因此生成期间界面照常
刷新、按键照常响应；取消时连接随协程一起关闭。
"""

from __future__ import annotations

import asyncio
import json
import ssl
from typing import Any, AsyncIterator, Sequence
from urllib.parse import urlsplit

from ..types import Chunk, ChunkKind, Message, ProviderConfig, Tool
from ..toolcalls import delta_from
from .openai import ProviderError

__all__ = ["HttpProvider"]

_DEFAULT_BASE_URL = "https://api.openai.com/v1"

# 单行的长度上限：防止对端发一行超长数据把内存吃光。
_MAX_LINE = 1024 * 1024

# 非 2xx 时最多读这么多字节的响应体用于报错。
_MAX_ERROR_BODY = 64 * 1024


def _usage(raw: dict[str, Any]) -> dict[str, int]:
    """把 OpenAI 的 usage 收成 ``prompt`` / ``completion`` 两个数。"""
    prompt = raw.get("prompt_tokens")
    completion = raw.get("completion_tokens")
    return {
        "prompt": int(prompt) if isinstance(prompt, int) else 0,
        "completion": int(completion) if isinstance(completion, int) else 0,
    }


class HttpProvider:
    """直接发 HTTP/1.1 请求的流式实现。"""

    def __init__(self, config: ProviderConfig) -> None:
        self.id = config.id
        self._config = config

    # ---- 请求 ----

    def _request_body(
        self,
        model: str,
        payload: list[dict[str, Any]],
        tools: Sequence[Tool] | None,
        parallel_tool_calls: bool | None,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        body = dict(self._config.params)  # type: Dict[str, Any]
        body.update(params)
        body["model"] = model
        body["messages"] = payload
        body["stream"] = True
        # 让对端在流末尾补一个 usage 事件：预算要靠它才对得准。
        body.setdefault("stream_options", {"include_usage": True})
        if tools:
            body["tools"] = [tool.to_payload() for tool in tools]
        if parallel_tool_calls is not None:
            body["parallel_tool_calls"] = parallel_tool_calls
        return body

    def _endpoint(self) -> str:
        base = (self._config.base_url or _DEFAULT_BASE_URL).rstrip("/")
        return base + "/chat/completions"

    def _headers(self, length: int) -> list[str]:
        url = urlsplit(self._endpoint())
        host = url.netloc
        lines = [
            "POST %s HTTP/1.1" % (url.path or "/"),
            f"Host: {host}",
            "Content-Type: application/json",
            "Accept: text/event-stream",
            f"Content-Length: {length}",
            # 不用分块传输，读到连接关闭即为结束；省掉客户端侧的 chunked 解码。
            "Connection: close",
        ]
        api_key = self._config.api_key
        if api_key:
            lines.append(f"Authorization: Bearer {api_key}")
        for name, value in self._config.headers.items():
            lines.append(f"{name}: {value}")
        return lines

    async def _connect(self):
        url = urlsplit(self._endpoint())
        if url.scheme not in ("http", "https") or not url.hostname:
            raise ProviderError(f"unsupported base_url: {self._endpoint()}")
        secure = url.scheme == "https"
        context = ssl.create_default_context() if secure else None
        port = url.port or (443 if secure else 80)
        try:
            return await asyncio.wait_for(
                asyncio.open_connection(
                    url.hostname,
                    port,
                    ssl=context,
                    server_hostname=url.hostname if secure else None,
                ),
                timeout=self._config.timeout,
            )
        except asyncio.TimeoutError:
            raise ProviderError(
                f"connect to {self._endpoint()} timed out after {self._config.timeout}s"
            ) from None
        except OSError as e:
            raise ProviderError(f"cannot reach {self._endpoint()}: {e}") from e

    @staticmethod
    async def _read_head(reader: asyncio.StreamReader):
        try:
            raw = await reader.readuntil(b"\r\n\r\n")
        except asyncio.IncompleteReadError as e:
            raise ProviderError("connection closed before response headers") from e
        lines = raw.decode("iso-8859-1").split("\r\n")
        status_parts = lines[0].split(" ", 2)
        if len(status_parts) < 2 or not status_parts[1].isdigit():
            raise ProviderError(f"malformed status line: {lines[0]!r}")
        status = int(status_parts[1])
        headers = {}
        for line in lines[1:]:
            if not line:
                continue
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
        return status, headers

    @staticmethod
    async def _iter_body(
        reader: asyncio.StreamReader, headers: dict[str, str]
    ) -> AsyncIterator[bytes]:
        """按传输方式交出响应体的原始字节。"""
        if headers.get("transfer-encoding", "").lower() == "chunked":
            while True:
                line = await reader.readuntil(b"\r\n")
                size = int(line.split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    return
                yield await reader.readexactly(size)
                await reader.readexactly(2)  # 块尾的 CRLF
        else:
            while True:
                data = await reader.read(8192)
                if not data:
                    return
                yield data

    @staticmethod
    async def _iter_sse(body: AsyncIterator[bytes]) -> AsyncIterator[dict[str, Any]]:
        """把响应体切成一条条 SSE 事件并解析成对象。"""
        pending = b""
        async for chunk in body:
            pending += chunk
            while b"\n" in pending:
                line, _, pending = pending.partition(b"\n")
                line = line.rstrip(b"\r")
                if not line.startswith(b"data:"):
                    continue  # 注释、event:、id: 都不需要
                data = line[5:].strip()
                if data == b"[DONE]":
                    return
                if not data:
                    continue
                try:
                    yield json.loads(data)
                except json.JSONDecodeError as e:
                    raise ProviderError(f"malformed stream chunk: {e}") from e

    # ---- 协议 ----

    async def chat(
        self,
        model: str,
        messages: Sequence[Message],
        *,
        tools: Sequence[Tool] | None = None,
        parallel_tool_calls: bool | None = None,
        **params: Any,
    ) -> AsyncIterator[Chunk]:
        body = self._request_body(
            model,
            [m.to_payload() for m in messages],
            tools,
            parallel_tool_calls,
            params,
        )
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = ("\r\n".join(self._headers(len(raw))) + "\r\n\r\n").encode("iso-8859-1")

        reader, writer = await self._connect()
        try:
            writer.write(request + raw)
            await writer.drain()

            status, headers = await self._read_head(reader)
            if status < 200 or status >= 300:
                detail = await reader.read(_MAX_ERROR_BODY)
                body = detail.decode("utf-8", "replace").strip()
                raise ProviderError(f"HTTP {status}: {body}")

            async for event in self._iter_sse(self._iter_body(reader, headers)):
                usage = event.get("usage")
                if isinstance(usage, dict):
                    yield Chunk(ChunkKind.USAGE, usage=_usage(usage))
                choices = event.get("choices") or ()
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                text = delta.get("content")
                if text:
                    yield Chunk(ChunkKind.TEXT, text)
                reasoning = delta.get("reasoning_content")
                if reasoning:
                    yield Chunk(ChunkKind.REASONING, reasoning)
                for fragment in delta.get("tool_calls") or ():
                    yield Chunk(
                        ChunkKind.TOOL_CALL_DELTA, fragment=delta_from(fragment)
                    )

            yield Chunk(ChunkKind.DONE)
        except ProviderError:
            raise
        except (asyncio.IncompleteReadError, ConnectionError) as e:
            raise ProviderError(f"connection lost while streaming: {e}") from e
        finally:
            writer.close()
