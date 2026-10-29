"""AI 模块的共享类型。

形状对齐 OpenAI chat completions：``messages`` 进，流式 delta 出，工具调用
按 ``tool_calls`` / ``tool_call_id`` 往返。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

__all__ = [
    "Chunk",
    "ChunkKind",
    "Message",
    "ProviderConfig",
    "Tool",
    "ToolCall",
    "ToolCallDelta",
]


@dataclass(frozen=True)
class ToolCall:
    """一次工具调用。

    ``arguments`` 保持原始 JSON 文本（协议如此），由调用方决定何时解析。
    """

    id: str
    name: str
    arguments: str = ""


@dataclass(frozen=True)
class ToolCallDelta:
    """工具调用的一个**分片**（wire 原样）。

    ``id`` / ``name`` 通常只在首片出现，``arguments`` 是 JSON 文本的一段。
    拼成完整调用由使用方做，见 :mod:`lls.ai.toolcalls`。
    """

    index: int = 0
    """同一次请求里的第几个调用（并行调用靠它分槽）。"""

    id: str = ""
    name: str = ""
    arguments: str = ""


@dataclass(frozen=True)
class Tool:
    """一个可调用的函数工具。"""

    name: str
    description: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)
    strict: bool = False

    def to_payload(self) -> dict[str, Any]:
        """转成请求体里的 tool 定义。"""
        function: dict[str, Any] = {
            "name": self.name,
            "parameters": self.parameters,
        }
        if self.description:
            function["description"] = self.description
        if self.strict:
            function["strict"] = True
        return {"type": "function", "function": function}


@dataclass(frozen=True)
class Message:
    """一条 chat 消息。"""

    role: str
    """system / user / assistant / tool。"""

    content: str | None = None
    """消息正文。带工具调用时为 None。"""

    tool_calls: tuple[ToolCall, ...] = ()
    """assistant 消息携带的工具调用。"""

    tool_call_id: str | None = None
    """tool 消息回应的那次调用。"""

    def to_payload(self) -> dict[str, Any]:
        """转成请求体里的一条消息。"""
        out: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_calls:
            out["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.arguments},
                }
                for call in self.tool_calls
            ]
        if self.tool_call_id is not None:
            out["tool_call_id"] = self.tool_call_id
        return out


class ChunkKind(str, Enum):
    """流式增量的种类。"""

    TEXT = "text"
    """正文（delta.content）。"""

    REASONING = "reasoning"
    """推理内容（delta.reasoning_content）。"""

    TOOL_CALL_DELTA = "tool_call_delta"
    """工具调用的一个分片，原样交出；归并见 :mod:`lls.ai.toolcalls`。"""

    USAGE = "usage"
    """本轮的 token 用量（``prompt`` / ``completion``）。"""

    DONE = "done"
    """本轮结束。"""


@dataclass(frozen=True)
class Chunk:
    """一段流式增量。"""

    kind: ChunkKind
    text: str = ""
    fragment: ToolCallDelta | None = None
    usage: dict[str, Any] | None = None


@dataclass(frozen=True)
class ProviderConfig:
    """一个 provider 实例的配置。"""

    id: str
    """在配置文件里作为键的标识。"""

    type: str
    """实现类型，决定用 providers/ 下哪个实现。"""

    name: str = ""
    """人类可读名。"""

    base_url: str | None = None
    api_key: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    timeout: float = 60.0
    models: list[str] = field(default_factory=list)
    params: dict[str, Any] = field(default_factory=dict)
    context_window: int = 128_000
    """模型的上下文窗口（token）；预算按它的 0.6 算。"""
