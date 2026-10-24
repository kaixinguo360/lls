"""通用 AI 模块：配置、provider 抽象、实例目录、多轮对话。

形状对齐 OpenAI chat completions。本模块不认识任何具体命令或终端概念，
上层（``lls.commands``）基于它构建功能。
"""

from __future__ import annotations

from .catalog import Catalog, parse_model_spec
from .config import AIConfig, ConfigError, load, resolve_config_path
from .conversation import Conversation
from .provider import Provider
from .registry import available_types, create_provider, register_type
from .types import Chunk, ChunkKind, Message, ProviderConfig, Tool, ToolCall

# 导入实现目录以触发内置 type 注册
from . import providers  # noqa: F401

__all__ = [
    "AIConfig",
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
    "load",
    "parse_model_spec",
    "providers",
    "register_type",
    "resolve_config_path",
]
