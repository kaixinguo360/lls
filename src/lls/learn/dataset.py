"""从 v2 会话构造 SFT 样本。

只读事件，不调模型、不碰终端：

- 只取 ``turn/end`` 明确完成且状态为 ``ok`` 的轮次；
- 用 :func:`lls.ai.sessions.events.fold` 还原消息，保留工具调用与结果；
- 去掉 ``ask_teacher`` 调用及其结果，训练样本里不出现「向老师求助」的痕迹；
- 每条样本带 system、tools、来源会话与轮次，方便复现和排查。
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from lls.ai import Message
from lls.ai.sessions import SessionStore
from lls.ai.sessions.events import Event, fold

__all__ = ["build_samples", "export", "read_system_text"]

SAMPLES = "sft.jsonl"
MANIFEST = "manifest.json"
SCHEMA = 1


def _body(event: Event) -> Dict[str, Any]:
    data = event.data
    return data if isinstance(data, dict) else {}


def _parts_text(parts: Any, read_blob: Any = None) -> str:
    if not isinstance(parts, list):
        return ""
    rows: List[str] = []
    for part in parts:
        if not isinstance(part, dict) or part.get("type") != "text":
            continue
        text = part.get("text")
        if isinstance(text, str) and text:
            rows.append(text)
            continue
        blob = part.get("blob")
        if isinstance(blob, str) and read_blob is not None:
            rows.append(read_blob(blob) or "")
    return "".join(rows)


def read_system_text(event: Event, read_blob: Any = None) -> str:
    """``system/message`` 的正文；v2 里正文是 parts。"""
    message = _body(event).get("message")
    if not isinstance(message, dict):
        return ""
    return _parts_text(message.get("content"), read_blob)


def _success(event: Optional[Event]) -> bool:
    if event is None:
        return False
    data = _body(event)
    reason = data.get("reason")
    status = data.get("lls", {}).get("status") if isinstance(data.get("lls"), dict) else ""
    kind = reason.get("kind") if isinstance(reason, dict) else ""
    return kind == "completed" and status == "ok"


def _strip_teacher(messages: Sequence[Message]) -> List[Message]:
    """删除求助调用与它的结果；其他消息原样保留。"""
    call_ids = set()
    for message in messages:
        if message.role == "assistant":
            for call in message.tool_calls:
                if call.name == "ask_teacher":
                    call_ids.add(call.id)
    if not call_ids:
        return list(messages)
    rows: List[Message] = []
    for message in messages:
        if message.role == "tool" and message.tool_call_id in call_ids:
            continue
        if message.role == "assistant" and any(
            call.name == "ask_teacher" for call in message.tool_calls
        ):
            calls = tuple(call for call in message.tool_calls if call.name != "ask_teacher")
            # 只为了求助而发的那条 assistant 整条丢掉；正文可能写着「我去问老师」，
            # 留在样本里会把求助习惯带进部署分布。
            if not calls:
                continue
            message = Message(
                message.role,
                message.content,
                tool_calls=calls,
                tool_call_id=message.tool_call_id,
            )
        rows.append(message)
    return rows


def _wire_message(message: Message) -> Dict[str, Any]:
    row: Dict[str, Any] = {"role": message.role, "content": message.content}
    if message.tool_calls:
        row["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.arguments},
            }
            for call in message.tool_calls
        ]
    if message.tool_call_id:
        row["tool_call_id"] = message.tool_call_id
    return row


def _has_conversation(messages: Sequence[Message]) -> bool:
    roles = {message.role for message in messages}
    return "user" in roles and "assistant" in roles


def _latest_before(
    events: Sequence[Event], kind: str, end_seq: int
) -> Optional[Event]:
    found = None
    for event in events:
        if event.type == kind and event.seq < end_seq:
            found = event
    return found


def build_samples(
    sessions_root: Path,
    *,
    include_failed: bool = False,
) -> Iterable[Dict[str, Any]]:
    """逐轮产出 ``{"messages", "tools", "meta"}``；只读。"""
    store = SessionStore(root=sessions_root)
    for meta in store.list():
        session = store.get(meta.id)
        if session is None:
            continue
        try:
            events = session.events()
            turns: Dict[int, List[Event]] = {}
            for event in events:
                if event.type in ("user/message", "assistant/message", "tool/result"):
                    turn = event.data.get("turn") if isinstance(event.data, dict) else None
                    if isinstance(turn, int):
                        turns.setdefault(turn, []).append(event)
            for turn in sorted(turns):
                content = turns[turn]
                end = next(
                    (
                        event
                        for event in events
                        if event.type == "turn/end"
                        and isinstance(event.data, dict)
                        and event.data.get("turn") == turn
                    ),
                    None,
                )
                if not include_failed and not _success(end):
                    continue
                messages = fold(content, blobs=session.read_blob)
                messages = _strip_teacher(messages)
                if not _has_conversation(messages):
                    continue
                system = _latest_before(events, "system/message", end.seq if end else 0)
                system_text = read_system_text(system, session.read_blob) if system else ""
                header = _latest_before(events, "request/header", end.seq if end else 0)
                tools = []
                if header is not None and isinstance(header.data, dict):
                    header_data = header.data.get("header")
                    if isinstance(header_data, dict) and isinstance(
                        header_data.get("tools"), list
                    ):
                        tools = header_data["tools"]
                patch = [_wire_message(message) for message in messages]
                if system_text:
                    patch.insert(0, {"role": "system", "content": system_text})
                yield {
                    "messages": patch,
                    "tools": tools,
                    "meta": {
                        "session": meta.id,
                        "turn": turn,
                        "model": "{}/{}".format(meta.provider_id, meta.model_id),
                        "created_at": meta.created_at,
                    },
                }
        finally:
            session.close()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def export(
    sessions_root: Path,
    out_dir: Path,
    *,
    include_failed: bool = False,
) -> Dict[str, Any]:
    """写 ``sft.jsonl`` 与 ``manifest.json``，返回 manifest。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / SAMPLES
    count = 0
    sessions = set()
    with path.open("w", encoding="utf-8") as handle:
        for sample in build_samples(sessions_root, include_failed=include_failed):
            handle.write(json.dumps(sample, ensure_ascii=False) + "\n")
            count += 1
            sessions.add(sample["meta"]["session"])
    manifest = {
        "schema": SCHEMA,
        "created_at": time.time(),
        "sessions_root": str(sessions_root),
        "sessions": len(sessions),
        "samples": count,
        "include_failed": include_failed,
        "samples_file": SAMPLES,
        "sha256": _sha256(path),
    }
    (out_dir / MANIFEST).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest
