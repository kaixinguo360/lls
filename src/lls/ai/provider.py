"""Provider 协议 —— 本模块唯一的扩展点。

只做一次 chat completion 的流式转发：``messages`` 进，``Chunk`` 流出。
工具调用以完整的 :class:`ToolCall` 形式产出，分片归并由实现方负责。
不认识提示词模板、不认识终端、不认识任何具体命令。
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Optional, Protocol, Sequence, runtime_checkable

from .types import Chunk, Message, Tool

__all__ = ["Provider"]


@runtime_checkable
class Provider(Protocol):
    """一个已配置好的 provider 实例。"""

    id: str
    """配置里的 id，用于报错与展示。"""

    def chat(
        self,
        model: str,
        messages: Sequence[Message],
        *,
        tools: Optional[Sequence[Tool]] = None,
        tool_choice: Optional[Any] = None,
        parallel_tool_calls: Optional[bool] = None,
        **params: Any,
    ) -> AsyncIterator[Chunk]:
        """执行一次流式 chat completion。

        ``params`` 为该实例的 ``params`` 与调用方覆盖项合并后的请求参数，
        原样透传给后端。实现方应返回**异步迭代器**。
        """
        ...
