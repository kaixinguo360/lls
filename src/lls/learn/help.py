"""``ask_teacher`` 工具：预算、冷却、脱敏，以及学生看到的建议格式。

教师本身在 :mod:`lls.learn.teacher`；这里只做工具层的事情，不拼请求、
不解析模型输出。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from lls.ai import AgentTool, Message
from lls.ai.types import Tool
from lls.learn.redact import redact
from lls.learn.teacher import TeacherReply

__all__ = ["ASK_TEACHER_TOOL", "HintLedger", "TranscriptView", "build_help_tool"]

ASK_TEACHER_TOOL = Tool(
    name="ask_teacher",
    description=(
        "向外部导师求助。导师不能执行任何命令，只能给诊断、下一步查什么、"
        "为什么以及如何验证；不会给通过/不通过。只在已经尝试过、确实卡住时"
        "使用；收到建议后自己验证。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "你试过什么、看到什么错误、怀疑什么。",
            }
        },
        "required": ["question"],
        "additionalProperties": False,
    },
    strict=True,
)


@dataclass
class TranscriptView:
    """本轮交给教师的消息；由 ``_Session`` 在流式过程中追加。"""

    messages: List[Message]

    def seed(self, messages: Sequence[Message]) -> None:
        self.messages = list(messages)

    def append(self, message: Message) -> None:
        self.messages.append(message)


class HintLedger:
    """按轮、按会话计数的求助预算。"""

    def __init__(self, max_per_turn: int, max_per_session: int, min_tool_calls: int):
        self.max_per_turn = max(0, int(max_per_turn))
        self.max_per_session = max(0, int(max_per_session))
        self.min_tool_calls = max(0, int(min_tool_calls))
        self.used_turn = 0
        self.used_session = 0
        self.tool_calls = 0

    def begin_turn(self) -> None:
        self.used_turn = 0
        self.tool_calls = 0

    def note_tool_call(self) -> None:
        self.tool_calls += 1

    def can_ask(self) -> Tuple[bool, str]:
        if self.used_turn >= self.max_per_turn:
            return False, "本轮求助次数已用完"
        if self.used_session >= self.max_per_session:
            return False, "本次会话求助次数已用完"
        if self.tool_calls < self.min_tool_calls:
            return False, "先自己尝试至少 {} 次工具调用再求助".format(
                self.min_tool_calls
            )
        return True, ""

    def record(self) -> None:
        self.used_turn += 1
        self.used_session += 1


def _redacted(message: Message, patterns: Sequence[str]) -> Message:
    calls = tuple(
        call.__class__(call.id, call.name, redact(call.arguments, patterns))
        for call in message.tool_calls
    )
    return Message(
        message.role,
        redact(message.content or "", patterns) if message.content else message.content,
        tool_calls=calls,
        tool_call_id=message.tool_call_id,
    )


def build_help_tool(
    teacher: Any,
    ledger: HintLedger,
    transcript: TranscriptView,
    *,
    redact_patterns: Sequence[str] = (),
    on_reply: Optional[Callable[[TeacherReply], None]] = None,
) -> AgentTool:
    async def run(arguments: Dict[str, Any]) -> str:
        question = arguments.get("question")
        if not isinstance(question, str) or not question.strip():
            return "error: 'question' must be a non-empty string"
        allowed, reason = ledger.can_ask()
        if not allowed:
            return "error: {}".format(reason)
        clean_question = redact(question.strip(), redact_patterns)
        messages = [_redacted(item, redact_patterns) for item in transcript.messages]
        reply = await teacher.advise(clean_question, messages)
        ledger.record()
        if on_reply is not None:
            on_reply(reply)
        index = ledger.used_turn
        total = ledger.max_per_turn
        return (
            "[导师建议 {}/{}]\n{}\n\n"
            "（导师不能执行，只能建议；请自己验证，并判断任务是否完成。）"
        ).format(index, total, reply.text)

    return AgentTool(ASK_TEACHER_TOOL, run)
