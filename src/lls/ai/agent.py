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
多少由调用方按 token 预算决定（见 :mod:`lls.ai.sessions.window`）。

``max_steps`` 默认 ``None``：不限制步数，直到模型不再调用工具。调用方也可以
给一个上限：到达时按 opencode 的做法收尾 —— 最后一轮禁用工具，并要求模型
只用文本总结。那条合成提示只存在于最后一次请求，不进入返回的会话历史。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Awaitable, Callable, Sequence

from .provider import Provider
from .toolcalls import ToolCallMerger
from .types import ChunkKind, Message, Tool, ToolCall

__all__ = [
    "Agent",
    "AgentCancelled",
    "AgentEvent",
    "AgentEventKind",
    "AgentResult",
    "AgentTool",
    "STOP_POLL",
    "stream_reply",
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
    run: Callable[[dict[str, Any]], Awaitable[str]]


class AgentEventKind(str, Enum):
    """进度事件的种类（只供 UI 使用）。"""

    STEP = "step"
    TEXT = "text"
    REASONING = "reasoning"
    CALL_DELTA = "call_delta"
    """工具调用分片：``name`` 是刚收到的名字，``text`` 是参数片段。"""
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

    messages: tuple[Message, ...]
    """会话消息（不含 system、不含收尾合成提示），可直接作为下一轮 history。"""

    usage: dict | None = None
    """最后一次模型请求的 token 用量；provider 没给就是 None。"""


class AgentCancelled(Exception):
    """外部要求中断（Ctrl-C、会话结束等）。"""




async def stream_reply(
    provider: Provider,
    model: str,
    messages: Sequence[Message],
    *,
    tools: Sequence[Tool] | None = None,
    step: int = 0,
    on_event: Callable[[AgentEvent], None] | None = None,
    on_text: Callable[[str], None] | None = None,
    poll: Callable[[float], Awaitable[None]] | None = None,
) -> tuple[str, list[ToolCall], dict | None]:
    """做一次流式请求，返回 ``(正文, 工具调用, 用量)``。

    这是**唯一**调用 ``Provider.chat`` 的地方：Agent 循环、历史摘要、输入
    生成都走它，于是「边收边报进度」「等的时候响应 Ctrl-C」只有一份实现。

    ``poll`` 在等待期间每 ``STOP_POLL`` 秒调用一次，由它负责等键盘，并在
    需要中断时抛 :class:`AgentCancelled`；中断时请求协程会被取消，连接随之
    关闭。``on_text`` 收到每一段正文（调用方据此保留中断前的半截答复）。
    """
    text_parts: list[str] = []
    merger = ToolCallMerger()
    usage: dict | None = None

    def emit(event: AgentEvent) -> None:
        if on_event is not None:
            on_event(event)

    async def consume() -> None:
        nonlocal usage
        async for chunk in provider.chat(
            model,
            messages,
            tools=tools,
            parallel_tool_calls=False if tools else None,
        ):
            if chunk.kind is ChunkKind.TEXT:
                text_parts.append(chunk.text)
                if on_text is not None:
                    on_text(chunk.text)
                emit(AgentEvent(AgentEventKind.TEXT, step=step, text=chunk.text))
            elif chunk.kind is ChunkKind.REASONING:
                emit(AgentEvent(AgentEventKind.REASONING, step=step, text=chunk.text))
            elif chunk.kind is ChunkKind.TOOL_CALL_DELTA and chunk.fragment is not None:
                merger.add(chunk.fragment)
                # 分片一到位就外传，界面能在流中看到「正在拼哪个调用」。
                emit(
                    AgentEvent(
                        AgentEventKind.CALL_DELTA,
                        step=step,
                        name=chunk.fragment.name,
                        text=chunk.fragment.arguments,
                    )
                )
            elif chunk.kind is ChunkKind.USAGE:
                usage = dict(chunk.usage or {})
            elif chunk.kind is ChunkKind.DONE:
                return

    await _drive(consume(), poll)
    return "".join(text_parts), merger.calls(), usage


async def _drive(
    work: Awaitable[None], poll: Callable[[float], Awaitable[None]] | None
) -> None:
    """等 ``work`` 做完；期间把键盘交给 ``poll``。

    中断（``poll`` 抛异常）时取消 ``work`` 并等它收尾：否则协程悬着，HTTP
    连接也不会立刻关闭。
    """
    task = asyncio.ensure_future(work)
    try:
        if poll is None:
            await task
        else:
            while not task.done():
                await poll(STOP_POLL)
            await task
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
class Agent:
    """「调用模型，执行工具，重复」的通用驱动。"""

    def __init__(
        self,
        provider: Provider,
        model: str,
        *,
        system: str,
        tools: Sequence[AgentTool],
        max_steps: int | None = None,
        step_limit_note: str = "",
        on_event: Callable[[AgentEvent], None] | None = None,
        poll: Callable[[float], Awaitable[None]] | None = None,
        on_message: Callable[[Message, dict | None], None] | None = None,
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
        # 每条进入会话的消息都回调一次（附带该轮的 usage）：调用方据此
        # 增量落盘，崩溃或中断时也不会丢掉已经发生的部分。
        self._on_message = on_message
        self._usage: dict | None = None
        self._partial = ""

    @property
    def partial_text(self) -> str:
        """本轮已经收到的正文。

        中断时正文还没凑完，也就没进会话；调用方拿它补一条 assistant，
        免得屏幕上已经显示的答复在上下文里凭空消失。
        """
        return self._partial

    # ---- 公开 ----

    async def run(
        self, task: str | None = None, history: Sequence[Message] = ()
    ) -> AgentResult:
        """执行一轮任务，直到模型不再调用工具（或达到可选的上限）。

        ``task`` 为 None 时不新增用户消息：接着 ``history`` 往下执行。
        """
        conversation = list(history)
        if task is not None:
            self._push(conversation, Message("user", task), None)

        step = 0
        while True:
            step += 1
            self._emit(AgentEvent(AgentEventKind.STEP, step=step))
            if self._max_steps is not None and step >= self._max_steps:
                return await self._finish_at_limit(conversation, step)

            text, calls = await self._round(
                step, self._request(conversation), self._tools_for_request()
            )
            self._push(conversation, self._assistant(text, calls), self._usage)
            # 这一轮的正文已经进会话了，中断时别再补一遍。
            self._partial = ""

            if not calls:
                # 没有工具调用 = 正常结束；文本就是最终答复（空文本也照常结束）。
                return AgentResult(
                    "completed", text, step, tuple(conversation), self._usage
                )

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
                self._push(
                    conversation, Message("tool", result, tool_call_id=call.id), None
                )

    # ---- 收尾 ----

    async def _finish_at_limit(
        self, conversation: list[Message], step: int
    ) -> AgentResult:
        """最后一轮：禁用工具，只让模型用文本总结。"""
        request = self._request(conversation) + [
            Message("user", STEP_LIMIT_TAG + "\n" + self._step_limit_note)
        ]
        text, _calls = await self._round(step, request, None)

        # 合成提示只用于这次请求：conversation 里从未出现它，最终总结照常保留。
        self._push(conversation, Message("assistant", text), self._usage)
        return AgentResult(
            "step_limit", text, step, tuple(conversation), self._usage
        )

    # ---- 一次模型请求 ----

    async def _round(
        self,
        step: int,
        messages: Sequence[Message],
        tools: Sequence[Tool] | None,
    ) -> tuple[str, list[ToolCall]]:
        """做一次流式请求：进度外传，正文留一份给中断时的补写。"""
        self._usage = None
        self._partial = ""
        text, calls, self._usage = await stream_reply(
            self._provider,
            self._model,
            messages,
            tools=tools,
            step=step,
            on_event=self._emit,
            on_text=self._keep_partial,
            poll=self._poll,
        )
        return text, calls

    def _keep_partial(self, text: str) -> None:
        """一收到正文就攥住：中断时还没凑完的答复要补进会话。"""
        self._partial += text

    # ---- 工具执行 ----

    async def _execute(self, step: int, call: ToolCall) -> str:
        entry = self._by_name.get(call.name)
        if entry is None:
            return "error: unknown tool {!r} (available: {})".format(
                call.name,
                ", ".join(sorted(self._by_name)) or "<none>",
            )

        try:
            arguments = json.loads(call.arguments or "{}")
        except (TypeError, ValueError) as e:
            return f"error: invalid tool arguments: {e}"
        if not isinstance(arguments, dict):
            return "error: tool arguments must be a JSON object"

        try:
            result = await entry.run(arguments)
        except AgentCancelled:
            raise
        except Exception as e:  # noqa: BLE001
            return f"error: {e}"

        if not isinstance(result, str):
            result = str(result)
        self._emit(
            AgentEvent(
                AgentEventKind.RESULT, step=step, name=call.name, text=result
            )
        )
        return result

    # ---- 辅助 ----

    def _request(self, conversation: Sequence[Message]) -> list[Message]:
        """请求消息 = 固定 system + 会话消息。"""
        return [Message("system", self._system)] + list(conversation)

    def _assistant(self, text: str, calls: Sequence[ToolCall]) -> Message:
        # 带工具调用时正文可为 None；纯文本答复即使为空也用空串（协议更稳）。
        content = (text or None) if calls else text
        return Message("assistant", content, tool_calls=tuple(calls))

    def _tools_for_request(self) -> Sequence[Tool] | None:
        if not self._tools:
            return None
        return [entry.tool for entry in self._tools]

    def _push(
        self, conversation: list[Message], message: Message, usage: dict | None
    ) -> None:
        """消息进会话的唯一入口：入列表，并把增量交给调用方。"""
        conversation.append(message)
        if self._on_message is not None:
            self._on_message(message, usage)

    def _emit(self, event: AgentEvent) -> None:
        if self._on_event is not None:
            self._on_event(event)
