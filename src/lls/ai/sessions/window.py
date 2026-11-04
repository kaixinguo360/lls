"""上下文窗口：按 token 预算从尾部裁剪，按「一步」切分。

预算与模型有关，与「几轮」无关：一次读大文件就能吃掉半窗，只数轮数根本
挡不住。这里只做纯计算，不认识磁盘也不发请求，便于单测。

切分的单位是「一步」，不是「一轮」：一轮里能跑几十步，每步都带回一段终端
画面或文件内容，一轮的体积可以顶满整个窗口。按轮切只剩两种结果 —— 整轮
留下（装不下，请求照样被 400），或者整轮丢掉（当前任务没了）。按步切才裁
得动，而且 ``assistant`` 与它那批 ``tool`` 回复永远落在同一块里，裁完不会
留下没人应答的 tool_calls。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Sequence

from ..types import Message
from .events import SUMMARY_PREFIX

__all__ = [
    "ASCII_PER_TOKEN",
    "BUDGET_RATIO",
    "COMPACT_TARGET_RATIO",
    "COMPACT_TRIGGER_RATIO",
    "NON_ASCII_TOKENS",
    "OUTPUT_RESERVE",
    "Window",
    "default_budget",
    "estimate_tokens",
    "fit",
    "message_tokens",
]

#: 粗估用的两个数：ASCII 四字符一个 token，非 ASCII 一个码点一个 token。
#: 只用于「要不要裁」的判断，宁可早裁一点，也不要请求被 400。
ASCII_PER_TOKEN = 4.0
NON_ASCII_TOKENS = 1.0

#: 预算占 context window 的比例，留给输出与工具定义。
BUDGET_RATIO = 0.6

#: 回复与工具定义要占的位置。比例已经留了四成，这里是一道硬底：小窗口上
#: 0.6 可能仍然装不下一次长回复（16k 窗口的六成是 9.6k，回复可能是 8k）。
OUTPUT_RESERVE = 8_192

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
        """一行用量摘要。

        只报数：压没压、为什么裁，都由调用方自己说，免得同一句话在一条
        消息里出现两遍。
        """
        parts = ["{} 条消息 / {}/{} tok".format(len(self.messages), self.used, self.budget)]
        if self.dropped:
            parts.append("裁掉 {} 条".format(self.dropped))
        if self.note:
            parts.append(self.note)
        return "，".join(parts)


def estimate_tokens(text: str) -> int:
    """按字符集粗估 token：ASCII 四字符一个，非 ASCII 一个码点一个。

    这个数只用来判断「要不要裁」，所以误差必须落在同一边：英文四五个字符
    才一个 token，汉字一个码点本来就要一个多。统一按字节折算会把中文压到
    0.86 token/字，水位永远到不了，等到请求被 400 才发现。宁可高估，
    早一点裁。
    """
    if not text:
        return 0
    # encode 两次都是 C 里的活：比逐字判断 ``ord(c) > 127`` 快得多。
    ascii_count = len(text.encode("ascii", "ignore"))
    non_ascii = len(text) - ascii_count
    return int(ascii_count / ASCII_PER_TOKEN + non_ascii * NON_ASCII_TOKENS) + 1


def message_tokens(message: Message) -> int:
    """一条消息的估算开销，含工具调用的名字与参数。"""
    total = _MESSAGE_OVERHEAD + estimate_tokens(message.role)
    total += estimate_tokens(message.content or "")
    for call in message.tool_calls:
        total += estimate_tokens(call.name) + estimate_tokens(call.arguments)
    return total


def default_budget(context_window: int, limit: int = 0) -> int:
    """预算 = min(窗口 * 0.6, 窗口 - 输出预留)；``limit`` 是配置里的硬上限。

    0.6 已经把四成留给输出与工具定义，正常情况下它就是答案。再拿
    ``窗口 - OUTPUT_RESERVE`` 卡一道是为了小窗口：16k 的模型按比例有 9.6k，
    可一次长回复就可能要 8k，两者相加正好把请求顶爆。窗口本身比预留还小
    （测试里的假窗口）时只按比例算，免得算出 0。
    """
    window = max(0, int(context_window or 0))
    budget = int(window * BUDGET_RATIO)
    if window > OUTPUT_RESERVE:
        budget = min(budget, window - OUTPUT_RESERVE)
    if limit and limit > 0:
        budget = min(budget, int(limit))
    return max(0, budget)


def fit(messages: Sequence[Message], budget: int, pending: Sequence[Message] = ()) -> Window:
    """从尾部向前装，装不下就整块丢掉。

    切分的单位是「步」：``user`` 自成一块，``assistant`` 连同它后面的
    ``tool`` 回复是一块。因此 assistant 的 tool_calls 与对应工具回复永远在
    同一块里，不会被拆散。

    ``pending`` 是这一轮一定要发、但还没进历史的消息（当前任务）。它占预算，
    但在没有历史（或全被裁掉）时保证请求里仍有内容可答；反过来，有 ``pending``
    时最近一块历史不再享受「必留」特权 —— 否则旧的大块会把当前任务挤出预算。
    """
    messages = list(messages)
    pending = list(pending)
    if not messages and not pending:
        return Window([], budget=budget, used=0)

    # 摘要固定在头部：它是被压缩掉的整段历史，再被裁掉就等于白压缩。
    head: List[Message] = []
    if messages and (messages[0].content or "").startswith(SUMMARY_PREFIX):
        head = [messages.pop(0)]

    steps = _split_steps(messages)
    kept: List[List[Message]] = []
    used = sum(message_tokens(message) for message in head)
    used += sum(message_tokens(message) for message in pending)
    for step in reversed(steps):
        cost = sum(message_tokens(message) for message in step)
        # 没有 pending 时最后一块历史必须留：它是这次请求唯一的输入。
        if budget > 0 and used + cost > budget and (kept or pending):
            break
        kept.append(step)
        used += cost
    kept.reverse()

    kept_count = sum(len(step) for step in kept)
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
    flat: List[Message] = head + [message for step in kept for message in step]
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


def _split_steps(messages: Sequence[Message]) -> List[List[Message]]:
    """按步切：``tool`` 跟着它前面那条 ``assistant`` 走，其余的各自起一块。"""
    steps: List[List[Message]] = []
    for message in messages:
        if message.role == "tool" and steps and steps[-1][0].role == "assistant":
            steps[-1].append(message)
        else:
            steps.append([message])
    return steps
