"""Transport —— OpenAI-compat，一个函数。

覆盖云端 + vLLM / llama.cpp / LM Studio（全都实现 /v1/chat/completions）。
HTTP 错误抛异常，不伪装成输出。不做提示词拼接、不做输出解析。
"""

from __future__ import annotations

import os
from typing import AsyncIterator

from .types import AIConfig, Chunk, ChunkKind, Message

__all__ = ["AIError", "ProviderError", "stream_chat"]


class AIError(Exception):
    """AI 层错误基类。"""


class ProviderError(AIError):
    """Provider 错误，带 endpoint 与可重试标记。"""

    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


async def stream_chat(
    cfg: AIConfig, messages: list[Message], *, stream: bool = True
) -> AsyncIterator[Chunk]:
    """流式调用 OpenAI-compat chat completions。

    :raises ProviderError: HTTP 层错误（401/429/5xx/网络）。
    :raises AIError: 配置缺失（无 api key）。
    """
    api_key = os.environ.get(cfg.api_key_env, "")
    if not api_key:
        raise AIError(
            f"未找到 API key：请设置环境变量 {cfg.api_key_env}"
        )

    try:
        from openai import AsyncOpenAI
    except ImportError as e:
        raise AIError("缺少 openai 依赖：pip install 'lls[openai]'") from e

    client = AsyncOpenAI(
        base_url=cfg.base_url,
        api_key=api_key,
        timeout=cfg.timeout,
    )
    payload = [{"role": m.role, "content": m.content} for m in messages]
    kwargs = {**cfg.params}
    if stream:
        kwargs["stream"] = True
    try:
        if stream:
            response = await client.chat.completions.create(
                model=cfg.model, messages=payload, **kwargs
            )
            async for piece in response:
                delta = piece.choices[0].delta if piece.choices else None
                if delta is None:
                    continue
                if delta.content:
                    yield Chunk(kind=ChunkKind.TEXT, text=delta.content)
                if getattr(delta, "reasoning_content", None):
                    yield Chunk(kind=ChunkKind.THINK, text=delta.reasoning_content)
        else:
            response = await client.chat.completions.create(
                model=cfg.model, messages=payload, **kwargs
            )
            content = response.choices[0].message.content if response.choices else ""
            yield Chunk(kind=ChunkKind.TEXT, text=content or "")
        yield Chunk(kind=ChunkKind.DONE)
    except AIError:
        raise
    except Exception as e:
        status = getattr(e, "status_code", None)
        retryable = status in (429, 500, 502, 503, 504) or status is None
        raise ProviderError(str(e), status=status, retryable=retryable) from e
