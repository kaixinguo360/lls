"""会话事件：一行一事件，只追加；消息与日志都由它折叠出来。

事件是**唯一事实来源**。读侧不缓存、不改写：``fold`` 折出送模型的消息，
``log_entries`` 折出覆盖层用的「任务/答复」两行。截断、压缩都发生在派生
视图上，磁盘上永远留着原文。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, List, Optional, Tuple

from ..types import Message, ToolCall

__all__ = [
    "Event",
    "CONTEXT_TYPES",
    "EVENT_TYPES",
    "INTERRUPT_MARKER",
    "MISSING_BLOB",
    "SUMMARY_PREFIX",
    "decode",
    "encode",
    "fold",
    "log_entries",
    "task_message",
]

#: 事件类型。``session_start`` / ``turn_end`` / ``model_switch`` / ``fork``
#: 是过程记录，不进上下文；``compact`` 替换某段区间。``screen`` 是某一轮
#: 随任务一起发出去的终端画面，由那条 ``user`` 的 ``screen`` 指过来 —— 它自己
#: 不是消息，只是那条消息的一部分。
EVENT_TYPES = frozenset(
    {
        "session_start",
        "user",
        "screen",
        "assistant",
        "tool",
        "turn_end",
        "compact",
        "model_switch",
        "fork",
    }
)

#: 折叠成消息的类型。
CONTEXT_TYPES = frozenset({"user", "assistant", "tool"})

#: 摘要顶在上下文最前面时用的提示，避免模型把它当成用户新提的任务。
SUMMARY_PREFIX = "（以下是更早对话的摘要，原文已压缩；需要细节时请问用户）\n"

#: 正文落在 blobs/ 里但读不出来时的占位：宁可留一句「已清理」，也不要
#: 静默跳过一个工具回复 —— 那会让 tool_calls 与 tool 对不上。
MISSING_BLOB = "（这段工具输出已清理，原文不可用）"

#: 被用户中断的轮次在上下文里留一行用户消息。和别家 CLI 的
#: 「[Request interrupted by user]」是同一件事：模型要知道上一轮是自己没说
#: 完、被打断的，而不是说完了，才知道该不该接着做。
INTERRUPT_MARKER = "（上一轮被用户中断，任务没做完；需要时接着做）"


@dataclass(frozen=True)
class Event:
    """一条会话事件。

    字段都是可选的：每种类型只用其中几项，编码时只写非默认的部分，
    一行因此能保持很短。
    """

    type: str
    seq: int = 0
    ts: float = 0.0
    # user / assistant：正文；tool：工具输出（大块的正文改存 blobs/）
    content: str = ""
    tool_calls: Tuple[ToolCall, ...] = ()
    tool_call_id: str = ""
    # turn_end：ok / aborted / error；tool：ok / error
    status: str = ""
    note: str = ""
    # assistant / session_start / model_switch：``provider/model``
    model: str = ""
    usage: Optional[dict] = None
    # user：typed / piped
    source: str = ""
    # user：随任务发出去的那条画面事件的 seq（0 = 没留）
    screen: int = 0
    # tool / screen：正文落在 blobs/ 时记摘要与字节数
    blob: str = ""
    size: int = 0
    # compact：摘要覆盖到哪一条 seq
    upto_seq: int = 0
    # fork：来源会话
    parent_id: str = ""


_TEXT_FIELDS = (
    "content",
    "tool_call_id",
    "status",
    "note",
    "model",
    "source",
    "blob",
    "parent_id",
)
_INT_FIELDS = ("screen", "size", "upto_seq")


def encode(event: Event) -> str:
    """编码成一行 JSON（不含换行）。"""
    payload: dict = {"type": event.type, "seq": event.seq, "ts": event.ts}
    for name in _TEXT_FIELDS:
        value = getattr(event, name)
        if value:
            payload[name] = value
    for name in _INT_FIELDS:
        value = getattr(event, name)
        if value:
            payload[name] = value
    if event.tool_calls:
        payload["tool_calls"] = [
            {"id": call.id, "name": call.name, "arguments": call.arguments}
            for call in event.tool_calls
        ]
    if event.usage:
        payload["usage"] = dict(event.usage)
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def decode(raw: Any) -> Event:
    """把一行解析成 :class:`Event`；形状不对就抛 ``ValueError``。"""
    if not isinstance(raw, dict):
        raise ValueError("event is not an object")
    kind = raw.get("type")
    if kind not in EVENT_TYPES:
        raise ValueError("unknown event type: {!r}".format(kind))
    seq = raw.get("seq")
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
        raise ValueError("event has no seq")
    values = {"type": kind, "seq": seq, "ts": _float(raw.get("ts"))}
    for name in _TEXT_FIELDS:
        value = raw.get(name, "")
        if not isinstance(value, str):
            raise ValueError("field {} is not a string".format(name))
        values[name] = value
    for name in _INT_FIELDS:
        value = raw.get(name, 0)
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError("field {} is not an int".format(name))
        values[name] = value
    values["tool_calls"] = tuple(_tool_call(item) for item in raw.get("tool_calls", ()))
    usage = raw.get("usage")
    if usage is not None and not isinstance(usage, dict):
        raise ValueError("usage is not an object")
    values["usage"] = usage
    return Event(**values)


def _tool_call(raw: Any) -> ToolCall:
    if not isinstance(raw, dict):
        raise ValueError("tool call is not an object")
    call_id = raw.get("id")
    name = raw.get("name")
    arguments = raw.get("arguments", "")
    if not isinstance(call_id, str) or not isinstance(name, str):
        raise ValueError("tool call has no id/name")
    if not isinstance(arguments, str):
        raise ValueError("tool call arguments is not a string")
    return ToolCall(id=call_id, name=name, arguments=arguments)


def _float(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


def fold(
    events: Iterable[Event],
    blobs: Optional[Callable[[Event], Optional[str]]] = None,
    marks: Optional[List[int]] = None,
) -> List[Message]:
    """把事件折成 ``Message`` 序列（不含 system）。

    ``compact`` 事件把 ``seq <= upto_seq`` 的区间换成一段摘要；工具输出
    落在 blobs 里时由 ``blobs`` 读回，读不到就用占位文本 —— 但绝不跳过
    消息，否则 tool_calls 会失去配对。

    中断和出错的轮次照样进上下文：用户看见屏幕上做到哪一步，模型也该看见，
    否则下一轮它只记得一句重述的任务，会把做过的事情再做一遍。缺的是收尾：
    没等到结果的工具调用补一条回复，中断的轮次补一行 :data:`INTERRUPT_MARKER`。

    每一轮 ``user`` 带上当时发出去的画面（:func:`task_message`）：重放出来的
    正文与当时的请求逐字相同，前面的历史才是稳定的前缀。

    给了 ``marks`` 时，同步记下每条消息来自哪一条 ``seq``（摘要那条记 0），
    压缩时据此算出「裁到哪一条」。
    """
    events = list(events)
    cutoff = 0
    summary = ""
    for event in events:
        if event.type == "compact" and event.upto_seq >= cutoff:
            cutoff, summary = event.upto_seq, event.content
    screens = {event.seq: event for event in events if event.type == "screen"}

    messages: List[Message] = []
    if marks is not None:
        marks.clear()
    if cutoff:
        messages.append(Message("user", SUMMARY_PREFIX + summary))
        if marks is not None:
            marks.append(0)
    # 发出去、还等结果的工具调用：(发起它的 seq, 调用 id)。中断和崩溃都会让
    # 它们永远等不到回复，而缺了配对的请求会被 API 拒掉。
    pending: List[Tuple[int, str]] = []

    def close(status: str = "") -> None:
        """给还在等结果的调用补一条回复。

        ``marks`` 记发起调用的那条 seq：合成出来的回复没有自己的事件，
        压缩时才不会把它和调用切开。
        """
        for seq, call_id in pending:
            messages.append(
                Message("tool", _cancelled_result(status), tool_call_id=call_id)
            )
            if marks is not None:
                marks.append(seq)
        pending.clear()

    # 压缩会把一轮的消息整段带走，它的 ``turn_end`` 却可能落在 cutoff 之后
    # （收尾不是消息，marks 指不到它）。这之前没有见过任何消息时，这条收尾
    # 属于已经压掉的那一轮，不能再把「被中断」的标记补到摘要后面。
    started = False
    for event in events:
        if event.seq <= cutoff:
            continue
        if event.type == "user":
            close()
            started = True
            messages.append(Message("user", _turn_text(event, screens, blobs)))
            if marks is not None:
                marks.append(event.seq)
        elif event.type == "assistant":
            started = True
            messages.append(
                Message(
                    "assistant",
                    event.content or None,
                    tool_calls=tuple(event.tool_calls),
                )
            )
            if marks is not None:
                marks.append(event.seq)
            pending.clear()
            pending.extend((event.seq, call.id) for call in event.tool_calls)
        elif event.type == "tool":
            started = True
            text = event.content
            if event.blob:
                text = (blobs(event) if blobs is not None else None) or MISSING_BLOB
            messages.append(Message("tool", text, tool_call_id=event.tool_call_id))
            if marks is not None:
                marks.append(event.seq)
            # 这条调用已经有结果，不用再补。
            pending[:] = [item for item in pending if item[1] != event.tool_call_id]
        elif event.type == "turn_end":
            if not started:
                continue
            close(event.status or "")
            if event.status == "aborted":
                messages.append(Message("user", INTERRUPT_MARKER))
                if marks is not None:
                    marks.append(event.seq)
    # 没有 turn_end 的残尾（进程被杀）也要收：新的一轮不该带着半截调用发出。
    close()
    return _drop_unpaired(messages)


def task_message(task: str, screen: str) -> str:
    """一轮交给模型的正文：先画面，后任务。

    写事件时由它拼请求，``fold`` 也由它重建 —— 模板只有这一份，重放才会
    与当时的请求逐字相同。
    """
    body = screen if screen.strip() else "（屏幕为空）"
    return (
        "<terminal_state>\n"
        + body
        + "\n</terminal_state>\n\n"
        + "<task>\n"
        + task
        + "\n</task>"
    )


def _turn_text(
    event: Event,
    screens: dict,
    blobs: Optional[Callable[[Event], Optional[str]]],
) -> str:
    """一条 ``user`` 在请求里的正文。

    画面取不到（没留、被压缩带走、blob 丢了）就退回任务原文：少一段输入
    只会让这一轮薄一点，不该变成另一条消息。
    """
    snapshot = screens.get(event.screen) if event.screen else None
    if snapshot is None:
        return event.content
    text = snapshot.content
    if snapshot.blob:
        text = blobs(snapshot) if blobs is not None else None
        if text is None:
            return event.content
    return task_message(event.content, text)


def _cancelled_result(status: str) -> str:
    """补在没等到结果的工具调用上的回复。

    分开写中断与出错：被中断的那次多半要接着做，出错的那次多半要重试，
    模型看到的理由不一样，下一步才不一样。
    """
    if status == "aborted":
        return "error: 这次调用被用户中断，没有结果"
    if status == "error":
        return "error: 这一轮出错，这次调用没有结果"
    return "error: 这次调用没有结果"


def _drop_unpaired(messages: List[Message]) -> List[Message]:
    """丢掉配对不全的 tool_calls 与其回复。

    ``fold`` 已经给没等到结果的调用补了回复，这里只兜底残破的日志：压缩把调用
    那一侧切掉了，或者回复比调用先出现。原样送进 API 会被 400 拒掉，所以整组
    一起丢，不留下半截。
    """
    out: List[Message] = []
    index = 0
    total = len(messages)
    while index < total:
        message = messages[index]
        if message.role == "assistant" and message.tool_calls:
            wanted = {call.id for call in message.tool_calls}
            cursor = index + 1
            answered = set()
            while cursor < total and messages[cursor].role == "tool":
                answered.add(messages[cursor].tool_call_id)
                cursor += 1
            if not wanted <= answered:
                index = cursor
                continue
            out.extend(messages[index:cursor])
            index = cursor
            continue
        if message.role == "tool":
            index += 1  # 没有对应调用的孤立回复
            continue
        out.append(message)
        index += 1
    return out


def log_entries(events: Iterable[Event], keep: int = 2) -> List[Tuple[str, str, str]]:
    """折出覆盖层用的 ``(任务, 答复, 备注)``，只留最近 ``keep`` 条。

    一轮可以分几段写：``/retry`` 接着跑的那一轮没有新的 ``user`` 事件，答复
    与 ``turn_end`` 直接跟在上一轮后面。这一段既不能丢（旧写法只认「有任务
    在手」，接着跑的那段全军覆没，覆盖层停在中断前的样子），也不能并进上一
    轮（``/status`` 要翻得到中断前已经写下的答复）：同名的任务另开一条。
    """
    entries: List[List[str]] = []
    task: Optional[str] = None     # 最近一条用户任务；续写的那一段沿用
    current: Optional[List[str]] = None  # 正在写的那一条；收尾后置空
    for event in events:
        if event.type == "user":
            task = event.content
            current = [task, "", ""]
            entries.append(current)
            continue
        if event.type == "assistant":
            if not event.content:
                continue
            if current is None:
                if task is None:
                    continue    # 切片开头的零散事件：没有可归的轮次
                current = [task, "", ""]
                entries.append(current)
            current[1] = event.content
        elif event.type == "turn_end":
            if current is None:
                if task is None:
                    continue
                current = [task, "", ""]
                entries.append(current)
            current[2] = event.note or ("" if event.status == "ok" else event.status)
            current = None
    if current is not None:
        current[2] = "未完成"
    return [(task, reply, note) for task, reply, note in entries][-max(1, keep):]
