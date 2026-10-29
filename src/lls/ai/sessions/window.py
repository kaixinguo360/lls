"""上下文窗口：按 token 预算从尾部裁剪，按轮切分。

预算与模型有关，与「几轮」无关：一次读大文件就能吃掉半窗，只数轮数根本
挡不住。这里只做纯计算，不认识磁盘也不发请求，便于单测。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, List, Sequence

from ..types import Message
from .events import SUMMARY_PREFIX

__all__ = [
    "BUDGET_RATIO",
    "CHARS_PER_TOKEN",
    "COMPACT_TARGET_RATIO",
    "COMPACT_TRIGGER_RATIO",
    "Window",
    "default_budget",
    "estimate_tokens",
    "fit",
    "last_turns",
    "message_tokens",
]

#: 粗估：一个 token 大约 3.5 个 UTF-8 字节。中文会低估，但只用于「要不要
#: 裁」的判断，宁可早裁一点，也不要请求被 400。
CHARS_PER_TOKEN = 3.5

#: 预算占 context window 的比例，留给输出与工具定义。
BUDGET_RATIO = 0.6

#: 压缩的触发水位：用到预算的这个比例就压，不等装不下才压。等到装不下，
#: 窗口会一直贴着上限：每轮只丢掉刚超出的那一点，于是每轮都要再压一次。
COMPACT_TRIGGER_RATIO = 0.85

#: 压缩的目标水位：一次压到这里，而不是只压够这一轮。触发线与目标线之差，
#: 就是两次压缩之间能攒下的上下文。
COMPACT_TARGET_RATIO = 0.6

#: 每条消息的固定开销（role、分隔符）。
_MESSAGE_OVERHEAD = 4


@dataclass(frozen=True)
class Window:
    """一次请求实际要带上的上下文，以及它是怎么被裁出来的。"""

    messages: List[Message] = field(default_factory=list)
    budget: int = 0
    used: int = 0
    dropped: int = 0
    """被裁掉的条数（不包含压缩掉的部分）。"""

    compacted: bool = False
    """是否用了 ``compact`` 摘要。"""

    keep_from: int = 0
    """输入里第一个被保留的下标（不含摘要）；等于输入长度表示全丢。"""

    dropped_tokens: int = 0
    """被裁掉部分的估算 token，用于判断值不值得压缩。"""

    note: str = ""
    """需要让用户知道的一句话（例如单轮就超预算）。"""

    def describe(self) -> str:
        """一行摘要，用于 turn_end 备注。"""
        parts = ["{} 条消息 / {}/{} tok".format(len(self.messages), self.used, self.budget)]
        if self.dropped:
            parts.append("裁掉 {} 条".format(self.dropped))
        if self.compacted:
            parts.append("已压缩")
        if self.note:
            parts.append(self.note)
        return "，".join(parts)


def estimate_tokens(text: str) -> int:
    """按 UTF-8 字节数粗估 token。"""
    if not text:
        return 0
    return int(len(text.encode("utf-8")) / CHARS_PER_TOKEN) + 1


def message_tokens(message: Message) -> int:
    """一条消息的估算开销，含工具调用的名字与参数。"""
    total = _MESSAGE_OVERHEAD + estimate_tokens(message.role)
    total += estimate_tokens(message.content or "")
    for call in message.tool_calls:
        total += estimate_tokens(call.name) + estimate_tokens(call.arguments)
    return total


def default_budget(context_window: int, limit: int = 0) -> int:
    """预算 = min(context window * 0.6, 配置上限)；上限为 0 表示不设。"""
    window = max(0, int(context_window or 0))
    budget = int(window * BUDGET_RATIO)
    if limit and limit > 0:
        budget = min(budget, int(limit))
    return max(0, budget)


def fit(messages: Sequence[Message], budget: int, pending: Sequence[Message] = ()) -> Window:
    """从尾部向前装，装不下就整轮丢掉。

    按 ``user`` 边界切分，因此 assistant 的 tool_calls 与对应 tool 回复
    永远在同一轮里，不会被拆散。

    ``pending`` 是这一轮一定要发、但还没进历史的消息（当前任务）。它占预算，
    但在没有历史（或全被裁掉）时保证请求里仍有内容可答；反过来，有 ``pending``
    时最近一轮历史不再享受「必留」特权 —— 否则旧的大轮次会把当前任务挤出预算。
    """
    messages = list(messages)
    pending = list(pending)
    if not messages and not pending:
        return Window([], budget=budget, used=0)

    # 摘要固定在头部：它是被压缩掉的整段历史，再被裁掉就等于白压缩。
    head: List[Message] = []
    if messages and (messages[0].content or "").startswith(SUMMARY_PREFIX):
        head = [messages.pop(0)]

    turns = _split_turns(messages)
    kept: List[List[Message]] = []
    used = sum(message_tokens(message) for message in head)
    used += sum(message_tokens(message) for message in pending)
    for turn in reversed(turns):
        cost = sum(message_tokens(message) for message in turn)
        # 没有 pending 时最后一条历史必须留：它是这次请求唯一的输入。
        if budget > 0 and used + cost > budget and (kept or pending):
            break
        kept.append(turn)
        used += cost
    kept.reverse()

    kept_count = sum(len(turn) for turn in kept)
    dropped = len(messages) - kept_count
    # 下标以调用方给的列表为准：摘要留在头部，因此要补上它的位置。
    offset = len(head)
    dropped_tokens = sum(
        message_tokens(message) for message in messages[: len(messages) - kept_count]
    )
    note = ""
    if budget > 0 and used > budget:
        note = "单轮已超预算"
    # ``pending`` 只参与预算：它由调用方自己随请求发出去，放进这里会重复一遍。
    flat: List[Message] = head + [message for turn in kept for message in turn]
    return Window(
        flat,
        budget=budget,
        used=used,
        dropped=dropped,
        # 头部是压缩摘要，说明这段历史被压过：``describe`` 据此说「已压缩」。
        compacted=bool(head),
        keep_from=offset + len(messages) - kept_count,
        dropped_tokens=dropped_tokens,
        note=note,
    )


def last_turns(messages: Sequence[Message], turns: int) -> List[Message]:
    """只留最近 ``turns`` 轮；``turns <= 0`` 表示不限制。"""
    messages = list(messages)
    if turns <= 0:
        return messages
    return [message for turn in _split_turns(messages)[-int(turns):] for message in turn]


def _split_turns(messages: Sequence[Message]) -> List[List[Message]]:
    turns: List[List[Message]] = []
    for message in messages:
        if message.role == "user" or not turns:
            turns.append([message])
        else:
            turns[-1].append(message)
    return turns
