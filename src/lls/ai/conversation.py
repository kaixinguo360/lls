"""多轮对话：按完整的「轮」累积消息。

一条 ``tool_calls`` 后面必须紧跟对应的 ``tool`` 回复，否则下一次请求的
``messages`` 就是非法的。因此这里以**轮**为单位存储：一轮要么完整进历史，
要么完全不进 —— 生成中途失败不会留下半条 assistant 消息。

本模块只管消息序列，不认识终端、不认识命令。
"""

from __future__ import annotations

from typing import List, Optional, Sequence

from .types import Message, ToolCall

__all__ = ["Conversation"]


class Conversation:
    """一段按轮累积的对话。

    ``max_turns`` 只保留最近的若干轮：每轮的 user 消息里带着当时的终端画面，
    无限累积会很快撑爆上下文。
    """

    def __init__(self, max_turns: int = 6) -> None:
        self._turns: List[List[Message]] = []
        self._max_turns = max(1, max_turns)

    def __len__(self) -> int:
        return len(self._turns)

    def messages(self) -> List[Message]:
        """把各轮摊平成一条消息序列。"""
        return [message for turn in self._turns for message in turn]

    def add_turn(self, user: Message, call: Optional[ToolCall], result: str) -> None:
        """记录一轮。

        ``call`` 为 None 表示这轮没经过模型（例如 ``请求#内容`` 直接给出），
        此时只留 user 消息 —— 没有问题，因为没有需要回应的工具调用。
        """
        turn = [user]
        if call is not None:
            turn.append(Message("assistant", None, tool_calls=(call,)))
            turn.append(Message("tool", result, tool_call_id=call.id))
        self._turns.append(turn)
        overflow = len(self._turns) - self._max_turns
        if overflow > 0:
            del self._turns[:overflow]

    def clear(self) -> None:
        self._turns.clear()
