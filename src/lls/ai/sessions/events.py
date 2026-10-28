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
    "MISSING_BLOB",
    "SUMMARY_PREFIX",
    "decode",
    "encode",
    "fold",
    "log_entries",
]

#: 事件类型。``session_start`` / ``turn_end`` / ``model_switch`` / ``fork``
#: 是过程记录，不进上下文；``compact`` 替换某段区间。
EVENT_TYPES = frozenset(
    {
        "session_start",
        "user",
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
    # tool：正文落在 blobs/ 时记摘要与字节数
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
_INT_FIELDS = ("size", "upto_seq")


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

    给了 ``marks`` 时，同步记下每条消息来自哪一条 ``seq``（摘要那条记 0），
    压缩时据此算出「裁到哪一条」。
    """
    events = list(events)
    # 中断/失败的轮次留在盘上备查，但不进上下文：下次请求不该看到
    # 一个没有答复的半截任务。
    skipped = _failed_ranges(events)
    cutoff = 0
    summary = ""
    for event in events:
        if event.type == "compact" and event.upto_seq >= cutoff:
            cutoff, summary = event.upto_seq, event.content

    messages: List[Message] = []
    if marks is not None:
        marks.clear()
    if cutoff:
        messages.append(Message("user", SUMMARY_PREFIX + summary))
        if marks is not None:
            marks.append(0)
    for event in events:
        if event.seq <= cutoff or _inside(event.seq, skipped):
            continue
        if event.type == "user":
            messages.append(Message("user", event.content))
            if marks is not None:
                marks.append(event.seq)
        elif event.type == "assistant":
            messages.append(
                Message(
                    "assistant",
                    event.content or None,
                    tool_calls=tuple(event.tool_calls),
                )
            )
            if marks is not None:
                marks.append(event.seq)
        elif event.type == "tool":
            text = event.content
            if event.blob:
                text = (blobs(event) if blobs is not None else None) or MISSING_BLOB
            messages.append(Message("tool", text, tool_call_id=event.tool_call_id))
            if marks is not None:
                marks.append(event.seq)
    return _drop_unpaired(messages)


def _failed_ranges(events: List[Event]) -> List[tuple]:
    """``turn_end`` 标了 aborted / error 的轮次：(起始 seq, 结束 seq)。"""
    ranges: List[tuple] = []
    start = 0
    for event in events:
        if event.type == "user":
            start = event.seq
        elif event.type == "turn_end":
            if start and event.status and event.status != "ok":
                ranges.append((start, event.seq))
            start = 0
    return ranges


def _inside(seq: int, ranges: List[tuple]) -> bool:
    for start, end in ranges:
        if start <= seq <= end:
            return True
    return False


def _drop_unpaired(messages: List[Message]) -> List[Message]:
    """丢掉配对不全的 tool_calls 与其回复。

    中断、崩溃都可能让 assistant 的调用没有回复；原样送进 API 会被 400
    拒掉，所以读侧统一清理：整组一起丢，不留下半截。
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
    """折出覆盖层用的 ``(任务, 答复, 备注)``，只留最近 ``keep`` 条。"""
    entries: List[Tuple[str, str, str]] = []
    task: Optional[str] = None
    reply = ""
    for event in events:
        if event.type == "user":
            task, reply = event.content, ""
        elif event.type == "assistant" and task is not None and event.content:
            reply = event.content
        elif event.type == "turn_end" and task is not None:
            note = event.note or ("" if event.status == "ok" else event.status)
            entries.append((task, reply, note))
            task, reply = None, ""
    if task is not None:
        entries.append((task, reply, "未完成"))
    return entries[-max(1, keep):]
