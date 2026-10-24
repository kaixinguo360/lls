"""Session —— 历史。存原料（Context + 指令），不存套过模板的 message。

这样「换 Role 重放同一段现场」才成立——这才是「保留原始输入以便扩展」的最终归宿。
Session 与 Role 正交：换 Role 重放同一 Session 是明确支持的行为。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .context import Context
from .role import Role
from .types import Message

__all__ = ["Turn", "Session"]


@dataclass(frozen=True, slots=True)
class Turn:
    """一轮对话：原料 + 指令 + 最终输出。"""

    instruct: str
    context_text: str        # Context 的文本视图（序列化友好）
    output: str              # 最终采纳的命令


@dataclass(slots=True)
class Session:
    id: str
    role_id: str = "chat"
    turns: list[Turn] = field(default_factory=list)

    def append(self, instruct: str, ctx: Context, output: str) -> None:
        self.turns.append(
            Turn(instruct=instruct, context_text=ctx.text, output=output)
        )

    def replay(self, role: Role, instruct: str, ctx: Context) -> list[Message]:
        """把历史 + 本次请求组装成 message 列表（换 Role 重放）。"""
        messages: list[Message] = []
        if role.system_prompt:
            messages.append(role.build_system())
        for t in self.turns:
            messages.append(Message(role="user", content=f"指令：{t.instruct}"))
            messages.append(Message(role="assistant", content=t.output))
        messages.append(role.build_user(instruct, ctx))
        return messages

    def to_jsonl(self) -> str:
        out = []
        for t in self.turns:
            out.append(
                json.dumps(
                    {"instruct": t.instruct, "context": t.context_text, "output": t.output},
                    ensure_ascii=False,
                )
            )
        return "\n".join(out)
