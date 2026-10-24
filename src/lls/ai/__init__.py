"""通用 AI 模块：配置、provider 抽象、实例目录、多轮对话与 agent 循环。

形状对齐 OpenAI chat completions。本模块不认识任何具体命令或终端概念，
上层（``lls.commands``）基于它构建功能。
"""

from __future__ import annotations

# 导入实现目录以触发内置 type 注册
from . import providers  # noqa: F401
from .agent import (
    Agent,
    AgentCancelled,
    AgentEvent,
    AgentEventKind,
    AgentResult,
    AgentTool,
    trim_turns,
)
from .catalog import Catalog, default_catalog, parse_model_spec
from .config import AIConfig, ConfigError, load, resolve_config_path
from .conversation import Conversation
from .provider import Provider
from .registry import available_types, create_provider, register_type
from .types import Chunk, ChunkKind, Message, ProviderConfig, Tool, ToolCall

__all__ = [
    "AIConfig",
    "Agent",
    "AgentCancelled",
    "AgentEvent",
    "AgentEventKind",
    "AgentResult",
    "AgentTool",
    "Catalog",
    "Chunk",
    "ChunkKind",
    "ConfigError",
    "Conversation",
    "Message",
    "Provider",
    "ProviderConfig",
    "Tool",
    "ToolCall",
    "available_types",
    "create_provider",
    "default_catalog",
    "load",
    "parse_model_spec",
    "providers",
    "register_type",
    "resolve_config_path",
    "trim_turns",
]
