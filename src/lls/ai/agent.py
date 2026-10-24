"""通用 Agent 循环：请求 → 工具调用 → 工具结果 → 再请求。

与具体后端、终端、UI 无关：

* provider 负责一次流式 chat（:class:`~lls.ai.Provider`）；
* 工具由调用方以 :class:`AgentTool` 提供（协议定义 + 执行函数），本模块不
  知道任何具体工具；
* 进度通过 ``on_event`` 外传；中断通过 ``poll`` 回调（抛
  :class:`AgentCancelled`）；
* **继续/结束只由「本轮是否出现工具调用」决定**：没有工具调用就是正常结束，
  assistant 文本即最终答复。这与主流 agent（DSH / opencode）一致 —— 不设
  finish 工具，也不把普通回复当成需要特殊处理的事件。

一次 :meth:`Agent.run` 就是一轮独立任务：``[system] + history + [user]``。
返回的 ``messages`` 不含 system，可以直接作为下一轮的 ``history``；跨轮保留
多少由调用方用 :func:`trim_turns` 决定。

``max_steps`` 默认 ``None``：不限制步数，直到模型不再调用工具。调用方也可以
给一个上限：到达时按 opencode 的做法收尾 —— 最后一轮禁用工具，并要求模型
只用文本总结。那条合成提示只存在于最后一次请求，不进入返回的会话历史。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

from .provider import Provider
from .types import ChunkKind, Message, Tool, ToolCall

__all__ = [
    "Agent",
    "AgentCancelled",
    "AgentEvent",
    "AgentEventKind",
    "AgentResult",
    "AgentTool",
    "STOP_POLL",
    "trim_turns",
]

# 等模型流式输出时，多久检查一次中断回调
STOP_POLL = 0.1

# 收尾提示的标记：它只用于最后一次请求，不进入会话历史
STEP_LIMIT_TAG = "<step-limit>"


@dataclass(frozen=True)
class AgentTool:
    """一个可被 agent 调用的工具：协议定义 + 执行函数。

    ``run`` 收到解析后的参数 dict，返回给模型的观察文本。它抛出的
    :class:`AgentCancelled` 会中断整个循环；其它异常会变成一条
    ``error: …`` 观察，让模型自行纠正。
    """

    tool: Tool
    run: Callable[[Dict[str, Any]], Awaitable[str]]


class AgentEventKind(str, Enum):
    """进度事件的种类（只供 UI 使用）。"""

    STEP = "step"
    TEXT = "text"
    REASONING = "reasoning"
    CALL = "call"
    RESULT = "result"


@dataclass(frozen=True)
class AgentEvent:
    """循环中的一次进度事件。"""

    kind: AgentEventKind
    step: int = 0
    name: str = ""
    text: str = ""


@dataclass(frozen=True)
class AgentResult:
    """一轮任务的结果。"""

    reason: str
    """``completed``（模型不再调用工具）或 ``step_limit``（达到步数上限）。"""

    summary: str
    """最终答复文本（可能为空）。"""

    steps: int
    """这一轮用掉的模型请求数。"""

    messages: Tuple[Message, ...]
    """会话消息（不含 system、不含收尾合成提示），可直接作为下一轮 history。"""


class AgentCancelled(Exception):
    """外部要求中断（Ctrl-C、会话结束等）。"""


class Agent:
    """「调用模型，执行工具，重复」的通用驱动。"""

    def __init__(
        self,
        provider: Provider,
        model: str,
        *,
        system: str,
        tools: Sequence[AgentTool],
        max_steps: Optional[int] = None,
        step_limit_note: str = "",
        on_event: Optional[Callable[[AgentEvent], None]] = None,
        poll: Optional[Callable[[float], Awaitable[None]]] = None,
    ) -> None:
        """``poll`` 在等待模型输出时反复调用，参数是建议等待的秒数。

        它应当真的等待（读输入/等信号），并在需要中断时抛
        :class:`AgentCancelled`；立即返回会让循环空转。
        """
        self._provider = provider
        self._model = model
        self._system = system
        self._tools = list(tools)
        self._by_name = {entry.tool.name: entry for entry in self._tools}
        # None = 不限步数；给了上限时至少两步：一步执行，一步收尾总结
        self._max_steps = None if max_steps is None else max(2, int(max_steps))
        self._step_limit_note = step_limit_note
        self._on_event = on_event
        self._poll = poll

    # ---- 公开 ----

    async def run(self, task: str, history: Sequence[Message] = ()) -> AgentResult:
        """执行一轮任务，直到模型不再调用工具（或达到可选的上限）。"""
        conversation = list(history)
        conversation.append(Message("user", task))

        step = 0
        while True:
            step += 1
            self._emit(AgentEvent(AgentEventKind.STEP, step=step))
            if self._max_steps is not None and step >= self._max_steps:
                return await self._finish_at_limit(conversation, step)

            text, calls = await self._round(
                step, self._request(conversation), self._tools_for_request()
            )
            conversation.append(self._assistant(text, calls))

            if not calls:
                # 没有工具调用 = 正常结束；文本就是最终答复（空文本也照常结束）。
                return AgentResult("completed", text, step, tuple(conversation))

            for call in calls:
                self._emit(
                    AgentEvent(
                        AgentEventKind.CALL,
                        step=step,
                        name=call.name,
                        text=call.arguments,
                    )
                )
                result = await self._execute(step, call)
                conversation.append(Message("tool", result, tool_call_id=call.id))

    # ---- 收尾 ----

    async def _finish_at_limit(
        self, conversation: List[Message], step: int
    ) -> AgentResult:
        """最后一轮：禁用工具，只让模型用文本总结。"""
        request = self._request(conversation) + [
            Message("user", STEP_LIMIT_TAG + "\n" + self._step_limit_note)
        ]
        text, _calls = await self._round(step, request, None)

        # 合成提示只用于这次请求：conversation 里从未出现它，最终总结照常保留。
        conversation.append(Message("assistant", text))
        return AgentResult("step_limit", text, step, tuple(conversation))

    # ---- 一次模型请求 ----

    async def _round(
        self,
        step: int,
        messages: Sequence[Message],
        tools: Optional[Sequence[Tool]],
    ) -> Tuple[str, List[ToolCall]]:
        """做一次流式请求，收集正文与工具调用。"""
        text_parts: List[str] = []
        calls: List[ToolCall] = []

        async def consume() -> None:
            async for chunk in self._provider.chat(
                self._model,
                messages,
                tools=tools,
                parallel_tool_calls=False if tools else None,
            ):
                if chunk.kind is ChunkKind.TEXT:
                    text_parts.append(chunk.text)
                    self._emit(
                        AgentEvent(AgentEventKind.TEXT, step=step, text=chunk.text)
                    )
                elif chunk.kind is ChunkKind.REASONING:
                    self._emit(
                        AgentEvent(AgentEventKind.REASONING, step=step, text=chunk.text)
                    )
                elif chunk.kind is ChunkKind.TOOL_CALL and chunk.call is not None:
                    calls.append(chunk.call)
                elif chunk.kind is ChunkKind.DONE:
                    return

        task = asyncio.ensure_future(consume())
        try:
            if self._poll is None:
                await task
            else:
                while not task.done():
                    await self._poll(STOP_POLL)
                await task
        finally:
            # 取消后必须收掉：否则协程悬着，HTTP 连接也不会立刻关闭。
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        return "".join(text_parts), calls

    # ---- 工具执行 ----

    async def _execute(self, step: int, call: ToolCall) -> str:
        entry = self._by_name.get(call.name)
        if entry is None:
            return "error: unknown tool %r (available: %s)" % (
                call.name,
                ", ".join(sorted(self._by_name)) or "<none>",
            )

        try:
            arguments = json.loads(call.arguments or "{}")
        except (TypeError, ValueError) as e:
            return "error: invalid tool arguments: %s" % e
        if not isinstance(arguments, dict):
            return "error: tool arguments must be a JSON object"

        try:
            result = await entry.run(arguments)
        except AgentCancelled:
            raise
        except Exception as e:  # noqa: BLE001
            return "error: %s" % e

        if not isinstance(result, str):
            result = str(result)
        self._emit(
            AgentEvent(
                AgentEventKind.RESULT, step=step, name=call.name, text=result
            )
        )
        return result

    # ---- 辅助 ----

    def _request(self, conversation: Sequence[Message]) -> List[Message]:
        """请求消息 = 固定 system + 会话消息。"""
        return [Message("system", self._system)] + list(conversation)

    def _assistant(self, text: str, calls: Sequence[ToolCall]) -> Message:
        # 带工具调用时正文可为 None；纯文本答复即使为空也用空串（协议更稳）。
        content = (text or None) if calls else text
        return Message("assistant", content, tool_calls=tuple(calls))

    def _tools_for_request(self) -> Optional[Sequence[Tool]]:
        if not self._tools:
            return None
        return [entry.tool for entry in self._tools]

    def _emit(self, event: AgentEvent) -> None:
        if self._on_event is not None:
            self._on_event(event)


def trim_turns(messages: Sequence[Message], max_turns: int) -> List[Message]:
    """只保留最近 ``max_turns`` 轮（每轮从一条 user 消息开始）。

    按 user 边界切分，因此 assistant 的 tool_calls 与对应的 tool 回复不会
    被拆散。``max_turns <= 0`` 返回空列表；不够 ``max_turns`` 轮时原样返回。
    """
    limit = int(max_turns)
    if limit <= 0:
        return []
    starts = [
        index for index, message in enumerate(messages) if message.role == "user"
    ]
    if len(starts) <= limit:
        return list(messages)
    return list(messages[starts[-limit]:])
