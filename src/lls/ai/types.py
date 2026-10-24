"""AI 层的共享类型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

__all__ = [
    "AIConfig",
    "Chunk",
    "ChunkKind",
    "Message",
]


class ChunkKind(str, Enum):
    """流式输出块的类型。"""

    TEXT = "text"        # 正文
    THINK = "think"      # 思维链（<think> 或原生 reasoning）
    USAGE = "usage"      # 用量统计
    DONE = "done"        # 结束标记
    ERROR = "error"      # 错误（通常直接抛异常，此处用于流内错误）


@dataclass(frozen=True, slots=True)
class Chunk:
    kind: ChunkKind
    text: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Message:
    """一条交给 transport 的消息。"""

    role: str  # system | user | assistant
    content: str


@dataclass(frozen=True, slots=True)
class AIConfig:
    """AI 端点配置（transport 层）。"""

    model: str = "gpt-4o-mini"
    base_url: str | None = None
    api_key_env: str = "LLS_OPENAI_API_KEY"
    timeout: float = 30.0
    params: dict[str, Any] = field(default_factory=dict)
