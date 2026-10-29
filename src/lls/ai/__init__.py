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
)
from .catalog import Catalog, default_catalog, parse_model_spec
from ..config import ConfigError, load_ai, resolve_config_path
from .config import AIConfig
from .conversation import Conversation
from .provider import Provider
from .registry import available_types, create_provider, register_type
from .toolcalls import ToolCallMerger
from .types import (
    Chunk,
    ChunkKind,
    Message,
    ProviderConfig,
    Tool,
    ToolCall,
    ToolCallDelta,
)

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
    "ToolCallDelta",
    "ToolCallMerger",
    "available_types",
    "create_provider",
    "default_catalog",
    "load",
    "load_ai",
    "parse_model_spec",
    "providers",
    "register_type",
    "resolve_config_path",
]

# 兼容旧调用：lls.ai.load 现在等同严格解析 AI 段。
load = load_ai
