"""会话落盘：一个会话一个目录，``events.jsonl`` 是唯一事实来源。

布局::

    <root>/<id>/meta.json      list 只读这个
    <root>/<id>/events.jsonl   追加写，一行一事件
    <root>/<id>/blobs/<sha1>   大块工具输出，事件里只留指针
    <root>/<id>/lock           fcntl 排他锁，内容是占用者的 pid

写事件不重写文件：崩溃最多丢最后一行，不会把整条会话损坏。``index.json``
只是「当前会话」的缓存，丢了按目录重扫即可。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import copy
import re
import secrets
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple, Union

from ...edit import forget_session
from ..types import Message, ToolCall
from .events import (
    META_TYPES,
    Event,
    decode,
    encode,
    fold,
    log_entries,
    reasoning_part,
    result_text,
    text_part,
    tool_call_part,
)
from .window import Window, fit

#: 事件 schema 的版本。旧版本会话不再读取，直接当作遗留数据。
SCHEMA_VERSION = 2

try:  # 只有 POSIX 有；没有就退化成无锁，单人使用仍能跑。
    import fcntl
except ImportError:  # pragma: no cover - 非 POSIX
    fcntl = None  # type: ignore[assignment]

__all__ = [
    "ARCHIVE_DIR",
    "BLOB_BYTES",
    "MAX_SESSIONS",
    "LegacySession",
    "SCHEMA_VERSION",
    "Session",
    "SessionBusy",
    "SessionError",
    "SessionMeta",
    "SessionStore",
    "default_sessions_dir",
    "new_session_id",
    "store",
]

META_NAME = "meta.json"
EVENTS_NAME = "events.jsonl"
BLOB_DIR = "blobs"
LOCK_NAME = "lock"
INDEX_NAME = "index.json"
ARCHIVE_DIR = "_archive"

#: 超过这个条数的新会话会把最久未用的移到 ``_archive/``：不删，只是不再
#: 出现在默认列表里。
MAX_SESSIONS = 20

#: 工具输出超过这么多字节就落 blobs/，事件里只留 sha1 与字节数。
BLOB_BYTES = 8 * 1024

#: 自动标题的长度。
TITLE_CHARS = 40

#: 会话 id 会直接做目录名：只接受生成器产出的形状，杜绝路径穿越。
_ID_RE = re.compile(r"^[0-9]{8}-[0-9]{4}-[0-9a-f]{6}$")

PathLike = Union[str, os.PathLike]


class SessionError(Exception):
    """会话不存在、无法解析或状态不对。"""


class LegacySession(SessionError):
    """会话写的是旧版事件 schema；运行时不再读取，也不做原地转换。"""

    def __init__(self, session_id: str, version: int) -> None:
        self.session_id = session_id
        self.version = version
        super().__init__(
            "会话 {} 使用旧版事件 schema（version {}），当前只支持 version {}；"
            "旧数据不迁移、不打开".format(session_id, version, SCHEMA_VERSION)
        )


class SessionBusy(SessionError):
    """另一个进程正开着这条会话。"""

    def __init__(self, session_id: str, pid: str = "") -> None:
        self.session_id = session_id
        self.pid = pid
        where = "pid {}".format(pid) if pid else "另一个进程"
        super().__init__("会话 {} 正被 {} 占用".format(session_id, where))


def new_session_id() -> str:
    """``20251028-1052-7d1075``：可读、按名排序即时间序。"""
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    return "{}-{}".format(stamp, secrets.token_hex(3))


def default_sessions_dir() -> Path:
    """会话目录：配置里的 ``paths.sessions_dir``（默认 ``~/.local/lls/sessions``）。"""
    from lls.config import load

    return load().paths.sessions_dir


@dataclass
class SessionMeta:
    """``meta.json`` 的内容：列表、续会、标题都只看它。"""

    id: str
    provider_id: str = ""
    model_id: str = ""
    title: str = ""
    cwd: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    parent_id: str = ""
    turns: int = 0
    last_seq: int = 0
    size: int = 0
    archived: bool = False
    tools: Dict[str, dict] = field(default_factory=dict)
    """会话层工具设置；空表示全部继承全局。"""

    @property
    def model(self) -> str:
        if not self.provider_id:
            return self.model_id
        return "{}/{}".format(self.provider_id, self.model_id)

    def to_json(self) -> dict:
        return {
            "version": SCHEMA_VERSION,
            "id": self.id,
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "title": self.title,
            "cwd": self.cwd,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "parent_id": self.parent_id,
            "turns": self.turns,
            "last_seq": self.last_seq,
            "size": self.size,
            "tools": self.tools,
        }


def _meta_from_json(raw: Any, session_id: str) -> SessionMeta:
    if not isinstance(raw, dict):
        raise ValueError("meta is not an object")
    if raw.get("id") not in (None, session_id):
        raise ValueError("meta id mismatch")
    version = raw.get("version", 1)
    if isinstance(version, bool) or not isinstance(version, int):
        raise ValueError("meta version is not an integer")
    if version != SCHEMA_VERSION:
        raise LegacySession(session_id, version)
    return SessionMeta(
        id=session_id,
        provider_id=_text(raw.get("provider_id")),
        model_id=_text(raw.get("model_id")),
        title=_text(raw.get("title")),
        cwd=_text(raw.get("cwd")),
        created_at=_number(raw.get("created_at")),
        updated_at=_number(raw.get("updated_at")),
        parent_id=_text(raw.get("parent_id")),
        turns=_integer(raw.get("turns")),
        last_seq=_integer(raw.get("last_seq")),
        size=_integer(raw.get("size")),
        tools=_tools_map(raw.get("tools")),
    )


def _tools_map(value: Any) -> Dict[str, dict]:
    """``meta.json`` 的 ``tools``：只收映射项，坏值丢弃。"""
    if not isinstance(value, dict):
        return {}
    return {
        str(name): item for name, item in value.items() if isinstance(item, dict)
    }


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


def _integer(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value


def _write_all(fd: int, payload: bytes) -> None:
    """写满整笔：``os.write`` 在管道上也可能短写。"""
    view = memoryview(payload)
    while view:
        view = view[os.write(fd, view):]


def _now_ms() -> int:
    return int(time.time() * 1000)


def _message_id() -> str:
    return secrets.token_hex(16)


def _model_id(model: str) -> str:
    return model.partition("/")[2] or model


def _parts_text(parts: Any) -> str:
    if not isinstance(parts, (list, tuple)):
        return ""
    return "".join(
        str(part.get("text") or "")
        for part in parts
        if isinstance(part, dict)
        and part.get("type") == "text"
        and isinstance(part.get("text"), str)
    )


def _ds_usage(usage: Optional[dict]) -> Dict[str, int]:
    """OpenAI 风格的 usage → DSH 的字段名；认不出的字段就丢弃。"""
    if not isinstance(usage, dict):
        return {}
    prompt = usage.get("prompt", usage.get("prompt_tokens", 0))
    completion = usage.get("completion", usage.get("completion_tokens", 0))
    if not isinstance(prompt, int) or isinstance(prompt, bool):
        prompt = 0
    if not isinstance(completion, int) or isinstance(completion, bool):
        completion = 0
    return {
        "inputTokens": prompt,
        "outputTokens": completion,
        "cacheReadTokens": 0,
        "reasoningTokens": 0,
        "totalTokens": prompt + completion,
    }


def _title_of(text: str) -> str:
    """首条 user 消息的开头做标题：压缩空白、截断。"""
    flat = " ".join(text.split())
    return flat[:TITLE_CHARS]


def _blob_digests(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "blob" and isinstance(item, str) and item:
                yield item
            else:
                for digest in _blob_digests(item):
                    yield digest
    elif isinstance(value, (list, tuple)):
        for item in value:
            for digest in _blob_digests(item):
                yield digest


def _copy_event_blobs(source: Path, target: Path, event: Event) -> None:
    """把事件里引用到的 blobs 一起复制过去；指针跟着事件走。"""
    digests = set(_blob_digests(dict(event.data)))
    if not digests:
        return
    target_dir = target / BLOB_DIR
    for digest in digests:
        path = source / BLOB_DIR / digest
        if not path.is_file():
            continue
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(str(path), str(target_dir / digest))
        except OSError:
            continue


def _upto(event: Event) -> int:
    value = event.data.get("upto_seq")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return 0


def _dropped(event: Event, ranges: List[Tuple[int, int]]) -> bool:
    """``event`` 落在某次撤销抹掉的区间里吗：``drop_from < seq < undo.seq``。

    元数据（请求快照、系统消息）不跟着撤销：它们是重建请求的依据。
    undo 自己那条不在区间内（上界是开区间），所以它仍然可读、可继续叠加。
    """
    if event.type in META_TYPES or event.type == "lls/undo":
        return False
    for low, high in ranges:
        if low < event.seq < high:
            return True
    return False


class Session:
    """一条已打开的会话：持有事件文件的写句柄与排他锁。"""

    def __init__(
        self,
        store: "SessionStore",
        path: Path,
        meta: SessionMeta,
        lock_fd: int,
    ) -> None:
        self.path = path
        self.meta = meta
        self._store = store
        self._lock_fd = lock_fd
        self._events_fd = _open_append(path / EVENTS_NAME)
        self.last_error: Optional[str] = None
        self.bad_lines = 0
        # 撤销抹掉的区间 ``(drop_from, undo 的 seq)``。懒加载：打开会话不必
        # 扫全量，真要过滤时才读一次；写一条 undo 就地补上。
        self._undos: Optional[List[Tuple[int, int]]] = None
        self._closed = False

    # ---- 元信息 ----

    @property
    def id(self) -> str:
        return self.meta.id

    @property
    def model(self) -> str:
        return self.meta.model

    def set_title(self, title: str) -> None:
        self.meta.title = " ".join(title.split())[:TITLE_CHARS]
        self.meta.updated_at = time.time()
        self._store.write_meta(self.meta)

    # ---- 追加 ----

    def _with_terminal(self, data: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        provider = self._store.terminal
        if provider is None:
            return data
        ref = provider()
        if ref is None:
            return data
        out = dict(data or {})
        lls = out.get("lls")
        if not isinstance(lls, dict):
            lls = {}
            out["lls"] = lls
        lls["terminal_cursor"] = {"recording_id": ref.recording, "frame": ref.frame}
        return out

    def append(
        self,
        kind: str,
        data: Optional[Dict[str, Any]] = None,
        *,
        surface_op: str = "",
        source_event_seqs: Iterable[int] = (),
    ) -> Optional[Event]:
        """追加一条事件；写失败只记 ``last_error``，不打断本轮。

        ``session`` 没有 ``seq`` / ``time``；其余事件失败时不推进 ``seq``，
        下次写入会补上同一个序号。
        """
        if self._closed:
            raise SessionError("会话 {} 已关闭".format(self.id))
        data = self._with_terminal(data)
        if kind == "session":
            event = Event(type=kind, data=dict(data or {}))
        else:
            event = Event(
                type=kind,
                seq=self.meta.last_seq + 1,
                time=_now_ms(),
                data=dict(data or {}),
                surface_op=surface_op,
                source_event_seqs=tuple(source_event_seqs),
            )
        line = (encode(event) + "\n").encode("utf-8")
        try:
            _write_all(self._events_fd, line)
        except OSError as e:
            self.last_error = "{}: {}".format(self.path / EVENTS_NAME, e)
            self._store.last_error = self.last_error
            return None
        if kind != "session":
            self.meta.last_seq = event.seq
        self.meta.size += len(line)
        self.meta.updated_at = time.time()
        if kind == "turn/end":
            self.meta.turns += 1
        if kind == "user/message" and not self.meta.title:
            self.meta.title = _title_of(_parts_text(event.data.get("content")))
        self._store.write_meta(self.meta)
        return event

    def append_screen(self, text: str, turn: int = 0) -> Optional[Event]:
        """记下这一轮随任务发出去的画面；大的画面落 blobs，与工具输出同一套。"""
        data: Dict[str, Any] = {"turn": turn}
        payload = text.encode("utf-8")
        if len(payload) <= BLOB_BYTES:
            data["content"] = text
        else:
            blob = self._write_blob(text)
            if blob:
                data.update({"content": "", "blob": blob, "size": len(payload)})
            else:
                data["content"] = text
        return self.append("lls/screen", data)

    def append_user(
        self, text: str, screen: int = 0, source: str = "typed"
    ) -> Optional[Event]:
        """记下这一轮的任务。``screen`` 是随它发出的那条画面事件的 seq。"""
        data: Dict[str, Any] = {
            "content": [text_part(text)],
            "source": {"kind": "user", "lls": {"source": source}},
            "role": "user",
            "id": _message_id(),
        }
        if screen:
            data["lls"] = {"screen_seq": screen}
        return self.append("user/message", data, surface_op="append")

    def append_system(
        self, text: str, turn: int = 0, step: int = 1, render_id: str = ""
    ) -> Optional[Event]:
        return self.append(
            "system/message",
            {
                "turn": turn,
                "step": step,
                "message": {
                    "role": "system",
                    "content": self._spill_parts([text_part(text)]),
                },
                "lls": {"render_id": render_id},
            },
        )

    def append_request_header(
        self, header: Dict[str, Any], render_id: str = ""
    ) -> Optional[Event]:
        return self.append(
            "request/header",
            {"header": header, "lls": {"render_id": render_id}},
        )

    def append_request_context(
        self,
        provider: str,
        model: str,
        context_window: int,
        budget: int,
        render_id: str = "",
    ) -> Optional[Event]:
        return self.append(
            "request/context",
            {
                "provider": provider,
                "model": model,
                "contextWindow": context_window,
                "lls": {"budget": budget, "render_id": render_id},
            },
        )

    def append_assistant(
        self,
        content: str,
        calls: Iterable[ToolCall] = (),
        reasoning: str = "",
        model: str = "",
        usage: Optional[dict] = None,
        turn: int = 0,
        step: int = 0,
    ) -> Optional[Event]:
        parts: List[dict] = []
        if reasoning:
            parts.append(reasoning_part(reasoning))
        if content:
            parts.append(text_part(content))
        for call in calls:
            parts.append(tool_call_part(call))
        data: Dict[str, Any] = {
            "turn": turn,
            "step": step,
            "message": {
                "role": "assistant",
                "content": self._spill_parts(parts),
                "source": {
                    "kind": "model",
                    "provider": self.meta.provider_id,
                    "model": _model_id(model),
                },
                "id": _message_id(),
            },
        }
        usage_payload = _ds_usage(usage)
        if usage_payload:
            data["usage"] = usage_payload
            if usage:
                data["lls"] = {"usage": dict(usage)}
        return self.append("assistant/message", data)

    def append_tool_call(self, call: ToolCall, turn: int = 0, step: int = 0) -> Optional[Event]:
        return self.append(
            "tool/call",
            {
                "turn": turn,
                "step": step,
                "callId": call.id,
                "name": call.name,
                "arguments": call.arguments,
            },
        )

    def append_tool_result(
        self,
        call: ToolCall,
        text: str,
        status: str = "ok",
        turn: int = 0,
        step: int = 0,
        source_seq: int = 0,
        meta: Optional[Dict[str, Any]] = None,
    ) -> Optional[Event]:
        """记录一次工具输出；大的正文落 blobs，事件里只留指针。"""
        data: Dict[str, Any] = {
            "turn": turn,
            "step": step,
            "message": {
                "role": "tool",
                "source": {"kind": "tool", "callId": call.id},
                "toolCallId": call.id,
                "content": self._spill_parts([text_part(text)]),
                "isError": status == "error",
                "id": _message_id(),
            },
            "meta": dict(meta or {}),
        }
        return self.append(
            "tool/result",
            data,
            surface_op="append",
            source_event_seqs=(source_seq,) if source_seq else (),
        )

    def append_turn_start(self, turn: int) -> Optional[Event]:
        return self.append("turn/start", {"turn": turn})

    def append_step_start(self, turn: int, step: int) -> Optional[Event]:
        return self.append("step/start", {"turn": turn, "step": step})

    def append_step_end(self, turn: int, step: int) -> Optional[Event]:
        return self.append("step/end", {"turn": turn, "step": step})

    def append_turn_end(
        self,
        status: str = "ok",
        note: str = "",
        usage: Optional[dict] = None,
        turn: int = 0,
    ) -> Optional[Event]:
        kind = {"ok": "completed", "aborted": "aborted", "error": "error"}.get(
            status, status
        )
        data: Dict[str, Any] = {
            "turn": turn,
            "reason": {"kind": kind},
            "lls": {"status": status},
        }
        if note:
            data["lls"]["note"] = note
        if usage:
            data["lls"]["usage"] = dict(usage)
        return self.append("turn/end", data)

    def append_compact(self, summary: str, upto_seq: int) -> None:
        self.append("lls/compact", {"summary": summary, "upto_seq": upto_seq})

    def append_prune(
        self, upto_seq: int, threshold: int, head: int, tail: int
    ) -> Optional[Event]:
        """记下一次修剪：``seq <= upto_seq`` 里超长的工具输出只留头尾。

        只记参数，不记改写过后的文本：内容由原文与预算确定性算出来，事件
        因此很小，重放（以及以后改常量）也永远得到同一份上下文。
        """
        return self.append(
            "lls/prune",
            {
                "upto_seq": int(upto_seq),
                "threshold": int(threshold),
                "head": int(head),
                "tail": int(tail),
            },
        )

    def append_undo(self, drop_from: int) -> Optional[Event]:
        """抹掉 ``seq > drop_from`` 的那一段：撤销上一轮。

        区间上界是这条 undo 自己的 seq，所以撤销之后重打一句再发出去的
        轮次照常可见。落盘的原文一个字不删 —— 撤销只改视图（见
        :meth:`visible_events`）。
        """
        tail = (
            self._from_end(lambda event: event.seq <= drop_from)
            if drop_from > 0
            else self.visible_events()
        )
        turns = sum(
            1 for event in tail if event.seq > drop_from and event.type == "turn/end"
        )
        event = self.append("lls/undo", {"drop_from": drop_from})
        if event is None:
            return None
        if self._undos is not None:
            self._undos.append((drop_from, event.seq))
        if turns:
            self.meta.turns = max(0, self.meta.turns - turns)
            self._store.write_meta(self.meta)
        return event

    def append_model_switch(self, model: str) -> None:
        self.append("lls/model-switch", {"model": model})

    def _spill_parts(self, parts: Iterable[Dict[str, Any]]) -> List[dict]:
        """把超长的文本 / 推理 part 落 blobs，事件里只留指针。"""
        out: List[dict] = []
        for part in parts:
            item = dict(part)
            text = item.get("text")
            if item.get("type") in ("text", "reasoning") and isinstance(text, str):
                payload = text.encode("utf-8")
                if len(payload) > BLOB_BYTES:
                    blob = self._write_blob(text)
                    if blob:
                        item["text"] = ""
                        item["blob"] = blob
                        item["size"] = len(payload)
            out.append(item)
        return out

    # ---- 读取 ----

    def events(self) -> List[Event]:
        """整份事件流；坏行（半行、格式不对）只计数，不打断读取。"""
        events: List[Event] = []
        for raw in self._lines():
            event = self._decode(raw)
            if event is not None:
                events.append(event)
        return events

    def recent(self) -> List[Event]:
        """上下文那一段：最近一次压缩覆盖到的位置之后，含那条摘要本身。

        压缩之后，前面的行只给分叉这类要原文的地方；每轮的
        上下文只由这一段派生，代价不随会话变长。摘要也可能写在它覆盖的位置
        之后（分叉就是这样），所以边界取 ``upto_seq`` 而不是摘要自己的 seq。
        """
        ranges = self._undo_ranges()
        events: List[Event] = []
        compact: Optional[Event] = None
        for raw in reversed(self._lines()):
            event = self._decode(raw)
            if event is None or _dropped(event, ranges):
                continue
            if compact is not None and event.seq <= _upto(compact):
                break
            events.append(event)
            if compact is None and event.type == "lls/compact" and _upto(event) > 0:
                compact = event
        events.reverse()
        return events

    def visible_events(self) -> List[Event]:
        """除去被撤销的那几段之后的事件流（原文仍在磁盘上）。"""
        ranges = self._undo_ranges()
        if not ranges:
            return self.events()
        return [event for event in self.events() if not _dropped(event, ranges)]

    def undo_point(self) -> Optional[Tuple[int, str]]:
        """上一轮从哪一条起、它说的什么；没有可撤销的轮次返回 ``None``。

        锚在 ``user/message`` 上：它每轮必写（``/retry`` 续跑的那一段没有
        它，本来就属于上一轮），要放回输入区的原文也只在它身上。``lls/screen``
        由 ``_submit`` 紧随其前写下，正常就一起带走；号对不上（没留、是 0、
        或比 user 还大）就只从 user 那一条起算，不跟着坏指针走。紧挨着它
        之前写的 ``lls/compact`` 也算这一轮的：自动压缩在提交任务之前发生，它
        只覆盖更早的历史，撤了这一轮却留着摘要，旧轮次就再也回不来了。
        """
        tail = self._from_end(lambda event: event.type == "user/message")
        if not tail or tail[0].type != "user/message":
            return None
        event = tail[0]
        screen = 0
        lls = event.data.get("lls")
        if isinstance(lls, dict):
            value = lls.get("screen_seq")
            if isinstance(value, int) and not isinstance(value, bool):
                screen = value
        start = screen if 0 < screen < event.seq else event.seq
        while start > 1:
            before = self._from_end(lambda item: item.seq < start)
            if not before or before[0].type != "lls/compact":
                break
            start = before[0].seq
        text = "".join(
            str(part.get("text") or "")
            for part in event.data.get("content", [])
            if isinstance(part, dict) and part.get("type") == "text"
        )
        return start, text

    def _undo_ranges(self) -> List[Tuple[int, int]]:
        """撤销抹掉的区间 ``(drop_from, undo 的 seq)``，按写入顺序。"""
        if self._undos is None:
            self._undos = [
                (int(event.data.get("drop_from", 0)), event.seq)
                for event in self.events()
                if event.type == "lls/undo"
            ]
        return self._undos

    def _lines(self) -> List[bytes]:
        path = self.path / EVENTS_NAME
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return []
        except OSError as e:
            self.last_error = "{}: {}".format(path, e)
            return []
        return [line for line in data.split(b"\n") if line.strip()]

    def _decode(self, raw: bytes) -> Optional[Event]:
        try:
            return decode(json.loads(raw))
        except (TypeError, ValueError):
            self.bad_lines += 1
            return None

    def _from_end(self, stop: Callable[[Event], bool]) -> List[Event]:
        """从尾部往回读，停在 ``stop`` 命中的那条（含它）。

        覆盖层要的那两行、上一次用量、最近一轮的收尾，都只在尾部：从后往前
        读，代价与真正要用的部分相当。
        """
        ranges = self._undo_ranges()
        events: List[Event] = []
        for raw in reversed(self._lines()):
            event = self._decode(raw)
            if event is None or _dropped(event, ranges):
                continue
            events.append(event)
            if stop(event):
                break
        events.reverse()
        return events

    def history(self) -> List[Message]:
        """折成送模型的消息；配对不全的 tool_calls 已由 fold 清理。"""
        return fold(self.recent(), self.read_blob)

    def log(self, keep: int = 2) -> List[tuple]:
        """覆盖层的最近 ``keep`` 条：从尾部往回找到够数的轮次为止。"""
        wanted = max(1, keep)
        tasks = 0

        def enough(event: Event) -> bool:
            nonlocal tasks
            if event.type == "user/message":
                tasks += 1
            return tasks > wanted

        return log_entries(self._from_end(enough), keep, self.read_blob)

    def history_marks(self) -> tuple:
        """消息 + 每条来自的 ``seq``；压缩时按它算裁到哪一条。"""
        marks: List[int] = []
        messages = fold(self.recent(), self.read_blob, marks)
        return messages, marks

    def compacted_upto(self) -> int:
        """最近一次压缩覆盖到哪一条 seq。"""
        tail = self._from_end(lambda event: event.type == "lls/compact")
        if tail and tail[0].type == "lls/compact":
            return _upto(tail[0])
        return 0

    def pruned_upto(self) -> int:
        """最近一次修剪覆盖到哪一条 seq。"""
        tail = self._from_end(lambda event: event.type == "lls/prune")
        if tail and tail[0].type == "lls/prune":
            return _upto(tail[0])
        return 0

    def oversized_results(self, after_seq: int, upto_seq: int, threshold: int) -> bool:
        """``after_seq < seq <= upto_seq`` 的工具输出里，有没有超长到该剪的？

        修剪要写一条事件，而事件一旦写下就再也不会被重算：先问一句，免得
        每次压缩都留下一刀「其实什么都没剪」的记录。
        """
        for event in self.recent():
            if event.type != "tool/result":
                continue
            if event.seq <= after_seq or event.seq > upto_seq:
                continue
            if len(result_text(event, self.read_blob)) > threshold:
                return True
        return False

    def window(self, budget: int) -> Window:
        return fit(self.history(), budget)

    def usage(self) -> Optional[dict]:
        """最近一次 ``assistant/message`` 记下的 usage。"""
        tail = self._from_end(
            lambda event: event.type == "assistant/message"
            and bool(event.data.get("usage") or event.data.get("lls"))
        )
        if tail and tail[0].type == "assistant/message":
            data = tail[0].data
            lls = data.get("lls")
            if isinstance(lls, dict) and isinstance(lls.get("usage"), dict):
                return dict(lls["usage"])
            raw = data.get("usage")
            if isinstance(raw, dict):
                return dict(raw)
        return None

    def last_turn_end(self) -> Optional[Event]:
        """最近一条收尾记录。"""
        tail = self._from_end(lambda event: event.type == "turn/end")
        if tail and tail[0].type == "turn/end":
            return tail[0]
        return None

    # ---- 内部 ----

    def _write_blob(self, text: str) -> str:
        digest = hashlib.sha1(text.encode("utf-8")).hexdigest()
        directory = self.path / BLOB_DIR
        path = directory / digest
        if path.exists():
            return digest
        try:
            directory.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(OSError):
                os.chmod(str(directory), 0o700)
            tmp = directory / (digest + ".tmp")
            with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
            with contextlib.suppress(OSError):
                os.chmod(str(tmp), 0o600)
            os.replace(str(tmp), str(path))
        except OSError as e:
            self.last_error = "{}: {}".format(directory, e)
            return ""
        return digest

    def read_blob(self, digest: str) -> Optional[str]:
        """读回一个内容块；供导出、回放等只读消费者使用。"""
        path = self.path / BLOB_DIR / digest
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._events_fd >= 0:
            with contextlib.suppress(OSError):
                os.close(self._events_fd)
            self._events_fd = -1
        if self._lock_fd >= 0:
            with contextlib.suppress(OSError):
                if fcntl is not None:
                    fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                os.close(self._lock_fd)
            self._lock_fd = -1
        self._store.forget(self.id)


class SessionStore:
    """会话目录的入口：开、建、列、删、分叉。

    ``root`` 为 None 时用一个临时目录：命令层的测试不必碰用户家目录。
    """

    def __init__(self, root: Optional[PathLike] = None, keep: int = MAX_SESSIONS) -> None:
        if root is None:
            self._root = Path(tempfile.mkdtemp(prefix="lls-sessions-"))
            self.temporary = True
        else:
            self._root = Path(root).expanduser()
            self.temporary = False
        self._keep = max(1, int(keep))
        self._open: Dict[str, Session] = {}
        self.terminal: Optional[Callable[[], Any]] = None
        self.last_error: Optional[str] = None
        self._index: Dict[str, Any] = {}
        self._read_index()

    @property
    def root(self) -> Path:
        return self._root

    # ---- 打开与创建 ----

    def set_terminal(self, provider: Optional[Callable[[], Any]]) -> None:
        self.terminal = provider

    def create(
        self,
        provider_id: str,
        model_id: str,
        *,
        title: str = "",
        parent_id: str = "",
    ) -> Session:
        """新建一条会话并设为当前会话。"""
        session_id = self._unused_id()
        path = self._root / session_id
        path.mkdir(parents=True, exist_ok=False)
        with contextlib.suppress(OSError):
            os.chmod(str(path), 0o700)
        meta = SessionMeta(
            id=session_id,
            provider_id=provider_id,
            model_id=model_id,
            title=title,
            cwd=os.getcwd(),
            created_at=time.time(),
            updated_at=time.time(),
            parent_id=parent_id,
        )
        self.write_meta(meta)
        session = self._open_dir(path, meta)
        session.append(
            "session",
            {
                "version": SCHEMA_VERSION,
                "id": session_id,
                "createdAt": int(meta.created_at * 1000),
                "cwd": meta.cwd,
                "isSeeded": False,
                "delegationDepth": 0,
                "agentPreset": "standard",
                "lls": {"app": _version(), "model": meta.model},
            },
        )
        self.set_current(session_id)
        # 建完再归档：先归档的话，刚建好的这一条会把列表撑到 keep + 1。
        self.archive_overflow()
        return session

    def open(self, spec: Optional[str] = None) -> Optional[Session]:
        """打开一条会话：``None`` 取当前会话，否则按 id / 前缀 / 标题片段。"""
        if spec is None or not spec.strip():
            session_id = self.current_id()
            if session_id is None:
                return None
            spec = session_id
        meta = self.resolve(spec)
        cached = self._open.get(meta.id)
        if cached is not None:
            return cached
        return self._open_dir(self._path_of(meta), meta)

    def get(self, spec: Optional[str]) -> Optional[Session]:
        try:
            return self.open(spec)
        except SessionError:
            return None

    def resolve(self, spec: str) -> SessionMeta:
        """按 id、唯一前缀或标题片段找一条会话。"""
        metas = self.list(include_archived=True)
        needle = spec.strip()
        for meta in metas:
            if meta.id == needle:
                return meta
        by_prefix = [meta for meta in metas if meta.id.startswith(needle)]
        if len(by_prefix) == 1:
            return by_prefix[0]
        by_title = [meta for meta in metas if needle.lower() in meta.title.lower()]
        if len(by_title) == 1:
            return by_title[0]
        if len(by_prefix) > 1:
            raise SessionError(
                "前缀 {} 匹配到多条会话：{}".format(
                    needle, ", ".join(meta.id for meta in by_prefix)
                )
            )
        if len(by_title) > 1:
            raise SessionError(
                "标题匹配到多条会话：{}".format(", ".join(meta.id for meta in by_title))
            )
        raise SessionError("没有匹配 {!r} 的会话".format(needle))

    # ---- 列表与当前 ----

    def list(self, include_archived: bool = False) -> List[SessionMeta]:
        """会话列表，最近更新的在前。"""
        metas: List[SessionMeta] = []
        for base, archived in self._roots(include_archived):
            try:
                entries = list(base.iterdir())
            except OSError:
                continue
            for path in entries:
                if not path.is_dir() or not _ID_RE.match(path.name):
                    continue
                meta = self._read_meta(path, archived)
                if meta is not None:
                    metas.append(meta)
        metas.sort(key=lambda meta: meta.updated_at, reverse=True)
        return metas

    def current_id(self) -> Optional[str]:
        current = self._index.get("current")
        if not isinstance(current, str) or not current:
            return None
        if not self._path_of_id(current).is_dir():
            return None
        return current

    def current(self) -> Optional[Session]:
        return self.open(None)

    def set_current(self, session_id: str) -> None:
        self._index = {"version": 1, "current": session_id}
        self._write_index()

    # ---- 删除、归档 ----

    def drop(self, spec: str) -> SessionMeta:
        meta = self.resolve(spec)
        session = self._open.pop(meta.id, None)
        if session is not None:
            session.close()
        shutil.rmtree(self._path_of(meta), ignore_errors=True)
        if self.current_id() == meta.id:
            self._index = {"version": 1, "current": ""}
            self._write_index()
        forget_session(meta.id)
        return meta

    def archive(self, spec: str) -> SessionMeta:
        """把一条会话移进 ``_archive/``：不删，``list --all`` 仍看得到。

        归档过的不再出现在默认列表里，也不再算进 ``keep`` 的条数 —— 它只是
        收起来，不是删掉。当前会话被归档时同样清掉 index 里的 current。
        """
        meta = self.resolve(spec)
        if meta.archived:
            return meta
        source = self._path_of(meta)
        archive = self._root / ARCHIVE_DIR
        try:
            archive.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(archive / meta.id))
        except OSError as e:
            raise SessionError("归档失败：{}: {}".format(source, e))
        meta.archived = True
        if self.current_id() == meta.id:
            self._index = {"version": 1, "current": ""}
            self._write_index()
        forget_session(meta.id)
        return meta

    def archive_overflow(self) -> List[str]:
        """超出 ``keep`` 的会话移到 ``_archive/``：不删，``list --all`` 仍在。"""
        metas = self.list()
        if len(metas) <= self._keep:
            return []
        archive = self._root / ARCHIVE_DIR
        archived: List[str] = []
        try:
            archive.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self.last_error = "{}: {}".format(archive, e)
            return []
        for meta in metas[self._keep:]:
            source = self._path_of(meta)
            target = archive / meta.id
            if target.exists():
                continue
            session = self._open.pop(meta.id, None)
            if session is not None:
                session.close()
            try:
                shutil.move(str(source), str(target))
            except OSError as e:
                self.last_error = "{}: {}".format(source, e)
                continue
            if self.current_id() == meta.id:
                self._index = {"version": 1, "current": ""}
                self._write_index()
            archived.append(meta.id)
        return archived

    # ---- 分叉与删除 ----

    def fork(self, spec: str, at_seq: int = 0) -> Session:
        """把 ``spec`` 在 ``at_seq`` 之前的事件复制成一条新会话；0 表示到末尾。"""
        source = self.open(spec)
        if source is None:
            raise SessionError("没有匹配 {!r} 的会话".format(spec))
        cutoff = at_seq if at_seq > 0 else source.meta.last_seq
        # 分叉抄的是看得见的那一段：被 /undo 抹掉的轮次不该跟着走。
        keep = [
            event
            for event in source.visible_events()
            if event.seq <= cutoff
            and event.type not in ("session", "lls/undo")
        ]
        new = self.create(
            source.meta.provider_id, source.meta.model_id, parent_id=source.id
        )
        moved: Dict[int, int] = {}
        for event in keep:
            _copy_event_blobs(source.path, new.path, event)
            data = copy.deepcopy(dict(event.data))
            lls = data.get("lls")
            if event.type == "user/message" and isinstance(lls, dict):
                screen = lls.get("screen_seq")
                if isinstance(screen, int) and not isinstance(screen, bool):
                    lls["screen_seq"] = moved.get(screen, 0)
            if event.type in ("lls/compact", "lls/prune"):
                upto = data.get("upto_seq")
                if isinstance(upto, int) and not isinstance(upto, bool):
                    covered = [
                        current for old, current in moved.items() if old <= upto
                    ]
                    data["upto_seq"] = max(covered) if covered else new.meta.last_seq
            sources = []
            for item in event.source_event_seqs:
                mapped = moved.get(item)
                if mapped:
                    sources.append(mapped)
            copied = new.append(
                event.type,
                data,
                surface_op=event.surface_op,
                source_event_seqs=sources,
            )
            if copied is not None:
                moved[event.seq] = copied.seq
        return new

    # ---- 内部 ----

    def forget(self, session_id: str) -> None:
        self._open.pop(session_id, None)

    def write_meta(self, meta: SessionMeta) -> None:
        path = self._path_of(meta) / META_NAME
        if not path.parent.is_dir():
            # 这条会话已经不在了（刚删掉、或被归档挪走）：别把它写回来 ——
            # 一次 write_meta 就能在原地长出一个空目录。
            self.forget(meta.id)
            return
        try:
            _atomic_write(path, json.dumps(meta.to_json(), ensure_ascii=False))
        except OSError as e:
            self.last_error = "{}: {}".format(path, e)
            session = self._open.get(meta.id)
            if session is not None:
                session.last_error = self.last_error

    def _open_dir(self, path: Path, meta: SessionMeta) -> Session:
        session = Session(self, path, meta, _lock(path / LOCK_NAME, meta.id))
        self._open[meta.id] = session
        return session

    def _roots(self, include_archived: bool) -> List[tuple]:
        roots: List[tuple] = [(self._root, False)]
        if include_archived:
            roots.append((self._root / ARCHIVE_DIR, True))
        return roots

    def _path_of(self, meta: SessionMeta) -> Path:
        base = self._root / ARCHIVE_DIR if meta.archived else self._root
        return base / meta.id

    def _path_of_id(self, session_id: str) -> Path:
        path = self._root / session_id
        if path.is_dir():
            return path
        return self._root / ARCHIVE_DIR / session_id

    def _read_meta(self, path: Path, archived: bool = False) -> Optional[SessionMeta]:
        try:
            raw = json.loads((path / META_NAME).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        try:
            meta = _meta_from_json(raw, path.name)
        except (LegacySession, TypeError, ValueError):
            return None
        meta.archived = archived
        return meta

    def _unused_id(self) -> str:
        for _ in range(100):
            candidate = new_session_id()
            if not (self._root / candidate).exists():
                return candidate
        raise SessionError("无法生成唯一的会话 id")

    def _read_index(self) -> None:
        try:
            raw = json.loads((self._root / INDEX_NAME).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(raw, dict):
            self._index = raw

    def _write_index(self) -> None:
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(OSError):
                os.chmod(str(self._root), 0o700)
            _atomic_write(
                self._root / INDEX_NAME, json.dumps(self._index, ensure_ascii=False)
            )
        except OSError as e:
            self.last_error = "{}: {}".format(self._root / INDEX_NAME, e)


# ---- 文件 ----

def _open_append(path: Path) -> int:
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with contextlib.suppress(OSError):
        os.chmod(str(path), 0o600)
    return fd


def _lock(path: Path, session_id: str) -> int:
    """取排他锁；已被占用时抛 :class:`SessionBusy`（带对方的 pid）。"""
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    if fcntl is None:
        return fd
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        pid = ""
        with contextlib.suppress(OSError):
            pid = os.read(fd, 32).decode("ascii", "replace").strip()
        os.close(fd)
        raise SessionBusy(session_id, pid) from None
    with contextlib.suppress(OSError):
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode("ascii"))
    return fd


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text + "\n")
    with contextlib.suppress(OSError):
        os.chmod(str(tmp), 0o600)
    os.replace(str(tmp), str(path))


def _version() -> str:
    from lls import __version__

    return __version__


_STORE: Optional[SessionStore] = None


def store() -> SessionStore:
    """进程内单例：跨多次 ``agent`` 调用共享同一份会话。"""
    global _STORE
    if _STORE is None:
        _STORE = SessionStore(root=default_sessions_dir())
    return _STORE
