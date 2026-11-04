"""会话事件：一行一事件，只追加；消息与日志都由它折叠出来。

事件是**唯一事实来源**。读侧不缓存、不改写：``fold`` 折出送模型的消息，
``log_entries`` 折出覆盖层用的「任务/答复」两行。截断、压缩都发生在派生
视图上，磁盘上永远留着原文。

存储形状与 DSH 的会话日志一致：除 ``session`` 外，一行是
``{"type", "seq", "time", "data"}``；消息内容用 parts 数组
（``text`` / ``reasoning`` / ``tool-call``），``time`` 是毫秒时间戳。
LLS 自己的东西放在 ``lls`` 命名空间里（``lls/screen`` 之类的事件类型、
``data.lls`` 里的附加字段），不往 DSH 的字段名里塞别的语义。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

from ..types import Message, ToolCall

__all__ = [
    "CONTEXT_TYPES",
    "EVENT_TYPES",
    "INTERRUPT_MARKER",
    "META_TYPES",
    "MISSING_BLOB",
    "PRUNE_MARKER",
    "SUMMARY_PREFIX",
    "Event",
    "decode",
    "encode",
    "fold",
    "log_entries",
    "message_from_parts",
    "prune_text",
    "reasoning_part",
    "result_text",
    "task_message",
    "text_part",
    "tool_call_part",
    "turn_status",
]

#: 事件类型。``session`` 的字段在顶层，其余事件都装进 ``data``。
EVENT_TYPES = frozenset(
    {
        "session",
        "turn/start",
        "turn/end",
        "step/start",
        "step/end",
        "system/message",
        "request/header",
        "request/context",
        "user/message",
        "assistant/message",
        "tool/call",
        "tool/result",
        "lls/screen",
        "lls/compact",
        "lls/prune",
        "lls/undo",
        "lls/model-switch",
    }
)

#: 折叠成消息的类型。
CONTEXT_TYPES = frozenset({"user/message", "assistant/message", "tool/result"})

#: 只用于重建请求、不进入上下文的元数据；撤销不隐藏它们。
META_TYPES = frozenset({"session", "system/message", "request/header", "request/context"})

#: 摘要顶在上下文最前面时用的提示，避免模型把它当成用户新提的任务。
SUMMARY_PREFIX = "（以下是更早对话的摘要，原文已压缩；需要细节时请问用户）\n"

#: 被修剪的工具输出：中间那段换成这句话。要写清省略了多少字 —— 模型才知道
#: 自己看到的不是全文，也才知道少了多少。
PRUNE_MARKER = "\n\n（这段工具输出的中间 {} 字已省略：只保留开头与结尾）\n\n"

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

    ``session`` 没有 ``seq`` / ``time``：它和 DSH 一样把字段放在顶层。
    其余事件一行一个，``data`` 是 JSON 对象。``surface_op`` 与
    ``source_event_seqs`` 是 DSH 的顶层字段，只出现在需要的地方。
    """

    type: str
    seq: int = 0
    time: int = 0
    data: Mapping[str, Any] = field(default_factory=dict)
    surface_op: str = ""
    source_event_seqs: Tuple[int, ...] = ()


def text_part(text: str) -> dict:
    return {"type": "text", "text": text}


def reasoning_part(text: str) -> dict:
    return {"type": "reasoning", "text": text}


def tool_call_part(call: ToolCall) -> dict:
    return {
        "type": "tool-call",
        "id": call.id,
        "name": call.name,
        "arguments": call.arguments,
    }


def _int(value: Any, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("{} must be an integer".format(where))
    return value


def _dict(value: Any, where: str) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("{} must be an object".format(where))
    return {str(key): item for key, item in value.items()}


def encode(event: Event) -> str:
    """编码成一行 JSON（不含换行）。"""
    if event.type == "session":
        payload: Dict[str, Any] = {"type": "session"}
        payload.update(dict(event.data))
    else:
        payload = {
            "type": event.type,
            "seq": event.seq,
            "time": event.time,
            "data": dict(event.data),
        }
        if event.surface_op:
            payload["surfaceOp"] = event.surface_op
        if event.source_event_seqs:
            payload["sourceEventSeqs"] = list(event.source_event_seqs)
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def decode(raw: Any) -> Event:
    """把一行解析成 :class:`Event`；形状不对就抛 ``ValueError``。"""
    if not isinstance(raw, dict):
        raise ValueError("event is not an object")
    kind = raw.get("type")
    if kind not in EVENT_TYPES:
        raise ValueError("unknown event type: {!r}".format(kind))
    if kind == "session":
        data = {str(key): value for key, value in raw.items() if key != "type"}
        version = data.get("version")
        if isinstance(version, bool) or not isinstance(version, int):
            raise ValueError("session has no version")
        if not isinstance(data.get("id"), str):
            raise ValueError("session has no id")
        return Event("session", data=data)

    seq = _int(raw.get("seq"), "seq")
    if seq < 1:
        raise ValueError("seq must be positive")
    time = _int(raw.get("time"), "time")
    if time < 0:
        raise ValueError("time must be non-negative")
    data = _dict(raw.get("data"), "data")
    surface_op = raw.get("surfaceOp", "")
    if not isinstance(surface_op, str):
        raise ValueError("surfaceOp must be a string")
    raw_sources = raw.get("sourceEventSeqs", ())
    if not isinstance(raw_sources, (list, tuple)):
        raise ValueError("sourceEventSeqs must be a list")
    sources = tuple(_int(item, "sourceEventSeqs") for item in raw_sources)
    return Event(
        type=kind,
        seq=seq,
        time=time,
        data=data,
        surface_op=surface_op,
        source_event_seqs=sources,
    )


def _part_text(
    part: Mapping[str, Any],
    blobs: Optional[Callable[[str], Optional[str]]],
) -> str:
    if not isinstance(part, Mapping):
        return ""
    blob = part.get("blob")
    if isinstance(blob, str) and blob:
        text = blobs(blob) if blobs is not None else None
        return text if text is not None else MISSING_BLOB
    text = part.get("text")
    return text if isinstance(text, str) else ""


def message_from_parts(
    parts: Any,
    *,
    role: str,
    blobs: Optional[Callable[[str], Optional[str]]] = None,
) -> Message:
    """把 DSH 的 parts 还原成 :class:`Message`。"""
    if not isinstance(parts, (list, tuple)):
        parts = []
    texts: List[str] = []
    calls: List[ToolCall] = []
    for part in parts:
        if not isinstance(part, Mapping):
            continue
        kind = part.get("type")
        if kind == "text":
            texts.append(_part_text(part, blobs))
        elif kind == "tool-call":
            call_id = part.get("id")
            name = part.get("name")
            arguments = part.get("arguments", "")
            if isinstance(call_id, str) and isinstance(name, str):
                if not isinstance(arguments, str):
                    arguments = json.dumps(arguments, ensure_ascii=False)
                calls.append(ToolCall(call_id, name, arguments))
    content = "".join(texts)
    if role == "assistant" and not content and calls:
        content = None
    return Message(role, content, tool_calls=tuple(calls))


def _message_data(event: Event) -> Mapping[str, Any]:
    data = event.data.get("message")
    return data if isinstance(data, Mapping) else {}


def _turn_status(event: Event) -> str:
    reason = event.data.get("reason")
    kind = reason.get("kind") if isinstance(reason, Mapping) else ""
    if kind == "completed":
        return "ok"
    if kind in ("aborted", "error"):
        return kind
    lls = event.data.get("lls")
    status = lls.get("status") if isinstance(lls, Mapping) else ""
    return status if isinstance(status, str) else ""


def turn_status(event: Event) -> str:
    """一条 ``turn/end`` 对 LLS 的 status：ok / aborted / error。"""
    return _turn_status(event)


def prune_text(text: str, threshold: int, head: int, tail: int) -> str:
    """头保尾留地剪一段工具输出；没超阈值就原样返回。

    预算以字符（码点）计，切片不会切坏代理对。内容已经在预算内就不动 ——
    修剪绝不能把结果改大，否则每压一次都要重剪一遍，还越剪越贵。

    留着开头是有原因的：命令、报错、路径一般都在前面；留着结尾是因为
    「最后一行是什么」几乎总是接着往下做时要看的那条信息。
    """
    if threshold <= 0 or head < 0 or tail < 0 or len(text) <= threshold:
        return text
    if head + tail >= len(text):
        return text
    omitted = len(text) - head - tail
    return text[:head] + PRUNE_MARKER.format(omitted) + text[-tail:]


def _prune_rule(events: Iterable[Event]) -> Tuple[int, int, int, int]:
    """最近一条 ``lls/prune`` 记下的规则：``(upto_seq, threshold, head, tail)``。

    取 ``upto_seq`` 最大的那条：分叉、撤销之后事件不再是严格递增的，但
    「盖得更远的那一刀」总是该生效的那一条。规则连同参数一起记在事件里，
    因此以后改常量也不会改写已有会话的重放结果。
    """
    rule = (0, 0, 0, 0)
    for event in events:
        if event.type != "lls/prune":
            continue
        data = event.data
        upto = data.get("upto_seq")
        threshold = data.get("threshold")
        head = data.get("head")
        tail = data.get("tail")
        if not _is_int(upto) or int(upto) <= 0:
            continue
        if not (_is_int(threshold) and _is_int(head) and _is_int(tail)):
            continue
        if int(upto) >= rule[0]:
            rule = (int(upto), int(threshold), int(head), int(tail))
    return rule


def _pruned(text: str, seq: int, rule: Tuple[int, int, int, int]) -> str:
    upto, threshold, head, tail = rule
    if not upto or seq > upto:
        return text
    return prune_text(text, threshold, head, tail)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def fold(
    events: Iterable[Event],
    blobs: Optional[Callable[[str], Optional[str]]] = None,
    marks: Optional[List[int]] = None,
) -> List[Message]:
    """把事件折成 ``Message`` 序列（不含 system）。

    ``lls/compact`` 把 ``seq <= upto_seq`` 的区间换成一段摘要；``lls/prune``
    把同一区间里超长的工具输出换成「开头 + 省略标记 + 结尾」。两者都是纯
    派生的视图：原文还在磁盘上，重放只按事件里记下的参数重算，因此同一条
    事件流永远折出同一份上下文。工具输出落在 blobs 里时由 ``blobs`` 读回，
    读不到就用占位文本 —— 但绝不跳过消息，否则 tool_calls 会失去配对。

    中断和出错的轮次照样进上下文：用户看见屏幕上做到哪一步，模型也该看见，
    否则下一轮它只记得一句重述的任务，会把做过的事情再做一遍。缺的是收尾：
    没等到结果的工具调用补一条回复，中断的轮次补一行 :data:`INTERRUPT_MARKER`。

    每一轮 ``user/message`` 带上当时发出去的画面（:func:`task_message`）：
    重放出来的正文与当时的请求逐字相同，前面的历史才是稳定的前缀。
    """
    events = list(events)
    cutoff = 0
    summary = ""
    for event in events:
        if event.type == "lls/compact":
            data = event.data
            upto = data.get("upto_seq")
            if isinstance(upto, int) and not isinstance(upto, bool) and upto >= cutoff:
                cutoff = upto
                summary = str(data.get("summary") or "")
    prune = _prune_rule(events)

    screens = {event.seq: event for event in events if event.type == "lls/screen"}
    messages: List[Message] = []
    if marks is not None:
        marks.clear()
    if cutoff:
        messages.append(Message("user", SUMMARY_PREFIX + summary))
        if marks is not None:
            marks.append(0)

    pending: List[Tuple[int, str]] = []

    def close(status: str = "") -> None:
        for seq, call_id in pending:
            messages.append(
                Message("tool", _cancelled_result(status), tool_call_id=call_id)
            )
            if marks is not None:
                marks.append(seq)
        pending.clear()

    started = False
    for event in events:
        if event.seq and event.seq <= cutoff:
            continue
        if event.type == "user/message":
            close()
            started = True
            text = "".join(
                _part_text(part, blobs)
                for part in event.data.get("content", [])
                if isinstance(part, Mapping) and part.get("type") == "text"
            )
            screen_seq = 0
            lls = event.data.get("lls")
            if isinstance(lls, Mapping):
                value = lls.get("screen_seq")
                if isinstance(value, int) and not isinstance(value, bool):
                    screen_seq = value
            snapshot = screens.get(screen_seq)
            if snapshot is not None:
                screen_data = snapshot.data
                blob = screen_data.get("blob")
                if isinstance(blob, str) and blob:
                    screen = blobs(blob) if blobs is not None else None
                    screen = screen if screen is not None else ""
                else:
                    value = screen_data.get("content")
                    screen = value if isinstance(value, str) else ""
                if screen.strip():
                    text = task_message(text, screen)
            messages.append(Message("user", text))
            if marks is not None:
                marks.append(event.seq)
        elif event.type == "assistant/message":
            started = True
            message = message_from_parts(
                _message_data(event).get("content", []),
                role="assistant",
                blobs=blobs,
            )
            messages.append(message)
            if marks is not None:
                marks.append(event.seq)
            pending.clear()
            pending.extend((event.seq, call.id) for call in message.tool_calls)
        elif event.type == "tool/result":
            started = True
            message = message_from_parts(
                _message_data(event).get("content", []),
                role="tool",
                blobs=blobs,
            )
            text = message.content or ""
            call_id = _tool_call_id(event)
            messages.append(
                Message("tool", _pruned(text, event.seq, prune), tool_call_id=call_id)
            )
            if marks is not None:
                marks.append(event.seq)
            pending[:] = [item for item in pending if item[1] != call_id]
        elif event.type == "turn/end":
            if not started:
                continue
            status = _turn_status(event)
            close(status)
            if status == "aborted":
                messages.append(Message("user", INTERRUPT_MARKER))
                if marks is not None:
                    marks.append(event.seq)
    close()
    return _drop_unpaired(messages)


def _tool_call_id(event: Event) -> str:
    message = _message_data(event)
    value = message.get("toolCallId")
    if isinstance(value, str):
        return value
    source = message.get("source")
    if isinstance(source, Mapping) and isinstance(source.get("callId"), str):
        return source["callId"]
    return ""


def _cancelled_result(status: str) -> str:
    if status == "aborted":
        return "error: 这次调用被用户中断，没有结果"
    if status == "error":
        return "error: 这一轮出错，这次调用没有结果"
    return "error: 这次调用没有结果"


def _drop_unpaired(messages: List[Message]) -> List[Message]:
    """丢掉配对不全的 tool_calls 与其回复。"""
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
            index += 1
            continue
        out.append(message)
        index += 1
    return out


def _entry_text(
    event: Event,
    blobs: Optional[Callable[[str], Optional[str]]],
) -> str:
    return "".join(
        _part_text(part, blobs)
        for part in event.data.get("content", [])
        if isinstance(part, Mapping) and part.get("type") == "text"
    )


def result_text(
    event: Event,
    blobs: Optional[Callable[[str], Optional[str]]] = None,
) -> str:
    """一条 ``tool/result`` 的正文（大的正文在 blobs 里时读回来）。"""
    return "".join(
        _part_text(part, blobs)
        for part in _message_data(event).get("content", [])
        if isinstance(part, Mapping) and part.get("type") == "text"
    )


def log_entries(
    events: Iterable[Event],
    keep: int = 2,
    blobs: Optional[Callable[[str], Optional[str]]] = None,
) -> List[Tuple[str, str, str]]:
    """折出覆盖层用的 ``(任务, 答复, 备注)``，只留最近 ``keep`` 条。"""
    entries: List[List[str]] = []
    task: Optional[str] = None
    current: Optional[List[str]] = None
    for event in events:
        if event.type == "user/message":
            task = _entry_text(event, blobs)
            current = [task, "", ""]
            entries.append(current)
            continue
        if event.type == "assistant/message":
            message = message_from_parts(
                _message_data(event).get("content", []),
                role="assistant",
                blobs=blobs,
            )
            text = message.content or ""
            if not text:
                continue
            if current is None:
                if task is None:
                    continue
                current = [task, "", ""]
                entries.append(current)
            current[1] = text
        elif event.type == "turn/end":
            if current is None:
                if task is None:
                    continue
                current = [task, "", ""]
                entries.append(current)
            lls = event.data.get("lls")
            note = lls.get("note") if isinstance(lls, Mapping) else ""
            status = _turn_status(event)
            current[2] = str(note or ("" if status == "ok" else status))
            current = None
    if current is not None:
        current[2] = "未完成"
    return [(task, reply, note) for task, reply, note in entries][-max(1, keep):]


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
