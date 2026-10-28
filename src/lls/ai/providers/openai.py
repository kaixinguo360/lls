"""OpenAI 兼容 provider。

走标准 ``/v1/chat/completions``。只做协议转发：``Message`` 列表变请求，
流式 delta 变 ``Chunk``；工具调用按 index 归并成完整的 ``ToolCall``。
不做提示词拼接、不做输出清洗。
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Sequence

from ..types import Chunk, ChunkKind, Message, ProviderConfig, Tool
from ._wire import finished_calls, merge_tool_fragments

__all__ = ["OpenAIProvider", "ProviderError"]


class ProviderError(Exception):
    """调用后端失败。"""


class OpenAIProvider:
    """基于 openai SDK 的流式实现。"""

    def __init__(self, config: ProviderConfig) -> None:
        self.id = config.id
        self._config = config

    # ---- 内部 ----

    def _client(self) -> Any:
        api_key = self._config.api_key
        if not api_key:
            raise ProviderError(
                f"provider {self.id!r} has no api_key"
            )
        try:
            from openai import AsyncOpenAI
        except ImportError as e:  # pragma: no cover - 依赖可选
            raise ProviderError(
                "openai package is required: pip install 'lls[ai]'"
            ) from e
        kwargs = {"api_key": api_key, "timeout": self._config.timeout}  # type: Dict[str, Any]
        if self._config.base_url:
            kwargs["base_url"] = self._config.base_url
        if self._config.headers:
            kwargs["default_headers"] = dict(self._config.headers)
        return AsyncOpenAI(**kwargs)

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
        client = self._client()
        payload = [m.to_payload() for m in messages]
        body = self._request_body(model, payload, tools, parallel_tool_calls, params)

        # index -> 累积中的调用。id/name 通常只在首片出现，arguments 逐片拼接。
        pending = {}  # type: Dict[int, Dict[str, str]]

        try:
            response = await client.chat.completions.create(**body)
            async for piece in response:
                usage = getattr(piece, "usage", None)
                if usage is not None:
                    yield Chunk(ChunkKind.USAGE, usage=_usage(usage))
                delta = self._delta(piece)
                if delta is None:
                    continue
                text = getattr(delta, "content", None)
                if text:
                    yield Chunk(ChunkKind.TEXT, text)
                reasoning = getattr(delta, "reasoning_content", None)
                if reasoning:
                    yield Chunk(ChunkKind.REASONING, reasoning)
                merge_tool_fragments(pending, getattr(delta, "tool_calls", None))
        except ProviderError:
            raise
        except Exception as e:  # noqa: BLE001
            raise ProviderError(str(e)) from e

        for call in finished_calls(pending):
            yield Chunk(ChunkKind.TOOL_CALL, call=call)

        yield Chunk(ChunkKind.DONE)

    # ---- 辅助 ----

    def _request_body(
        self,
        model: str,
        payload: list[dict[str, Any]],
        tools: Sequence[Tool] | None,
        parallel_tool_calls: bool | None,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """拼出请求体。"""
        body = dict(self._config.params)  # type: Dict[str, Any]
        body.update(params)
        body["model"] = model
        body["messages"] = payload
        body["stream"] = True
        # 流末尾补一个 usage 事件：预算要靠它才对得准。
        body.setdefault("stream_options", {"include_usage": True})

        if tools:
            body["tools"] = [tool.to_payload() for tool in tools]
        if parallel_tool_calls is not None:
            body["parallel_tool_calls"] = parallel_tool_calls
        return body

    @staticmethod
    def _delta(piece: Any) -> Any:
        choices = getattr(piece, "choices", None)
        if not choices:
            return None
        return getattr(choices[0], "delta", None)


def _usage(raw: Any) -> dict[str, int]:
    """把 SDK 的 usage 收成 ``prompt`` / ``completion`` 两个数。"""
    prompt = getattr(raw, "prompt_tokens", 0) or 0
    completion = getattr(raw, "completion_tokens", 0) or 0
    return {"prompt": int(prompt), "completion": int(completion)}
