#!/usr/bin/env python3
"""把旧的 ``<id>.json`` 会话转换成 ``<id>/events.jsonl`` 布局（一次性）。

旧格式（``commands/builtin/agent_store.py``）把整份状态写在一个文件里：
``history`` 是扁平的 ``Message`` 列表，``log`` 只是覆盖层用的最近几条提示。
新格式只认事件流，``history`` / ``log`` 都由 ``fold()`` 现算。因此转换就是
把每条历史消息拆回事件：

    session_start            一条，记 provider/model
    user / assistant / tool  与 history 一一对应，tool 的大正文落 blobs/
    turn_end                 每条以 user 开头的轮次末尾补一条，status=ok

旧盘里只会存下已经完成的一轮（``_save`` 只在轮末调用），所以一律记 ok；
旧 ``log`` 里最多两条 note 按「任务原文」从尾部对齐，补到对应 turn_end。
id 由 ``created_at`` 的本地时间加上旧 id 组成，重复运行结果一致；目标目录
已存在就跳过。旧 ``<id>.json`` 原样保留，确认新会话能打开后自行删除。

不做的：不压缩旧历史（新窗口会自己裁），不造 compact 事件，不改旧文件。

用法::

    python3 migrate_sessions.py                # 默认会话目录
    python3 migrate_sessions.py --root DIR     # 指定目录
    python3 migrate_sessions.py --dry-run      # 只报告，不写盘
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "src"))

from lls import __version__  # noqa: E402
from lls.ai.sessions.events import Event, encode  # noqa: E402
from lls.ai.sessions.store import (  # noqa: E402
    BLOB_BYTES,
    BLOB_DIR,
    EVENTS_NAME,
    INDEX_NAME,
    META_NAME,
    TITLE_CHARS,
    default_sessions_dir,
)
from lls.ai.types import Message, ToolCall  # noqa: E402

OLD_SUFFIX = ".json"
OLD_ID_RE = re.compile(r"^[0-9a-f]{4,64}$")
SUFFIX_RE = re.compile(r"^[0-9a-f]{6}$")
SCHEMA_VERSION = 1


class OldSessionError(Exception):
    """旧文件结构不对：整条跳过，不写半成品。"""


def _number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


def _tool_call(raw: Any) -> ToolCall:
    if not isinstance(raw, dict):
        raise OldSessionError("tool call 不是对象")
    call_id, name = raw.get("id"), raw.get("name")
    arguments = raw.get("arguments", "")
    if not isinstance(call_id, str) or not isinstance(name, str):
        raise OldSessionError("tool call 缺少 id/name")
    if not isinstance(arguments, str):
        raise OldSessionError("tool call 的 arguments 不是字符串")
    return ToolCall(id=call_id, name=name, arguments=arguments)


def _message(raw: Any) -> Message:
    if not isinstance(raw, dict):
        raise OldSessionError("message 不是对象")
    role = raw.get("role")
    if role not in ("user", "assistant", "tool"):
        raise OldSessionError("未知角色：{!r}".format(role))
    content = raw.get("content")
    if content is not None and not isinstance(content, str):
        raise OldSessionError("content 不是字符串")
    calls = raw.get("tool_calls", [])
    if not isinstance(calls, list):
        raise OldSessionError("tool_calls 不是列表")
    call_id = raw.get("tool_call_id")
    if call_id is not None and not isinstance(call_id, str):
        raise OldSessionError("tool_call_id 不是字符串")
    return Message(
        role=role,
        content=content,
        tool_calls=tuple(_tool_call(item) for item in calls),
        tool_call_id=call_id,
    )


def load_old(path: Path) -> Dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise OldSessionError(str(e)) from None
    if not isinstance(raw, dict):
        raise OldSessionError("不是一个 JSON 对象")
    if raw.get("version") != SCHEMA_VERSION:
        raise OldSessionError("版本不对：{!r}".format(raw.get("version")))
    provider_id, model_id = raw.get("provider_id"), raw.get("model_id")
    if not isinstance(provider_id, str) or not isinstance(model_id, str):
        raise OldSessionError("缺少 provider/model")
    history = raw.get("history", [])
    log = raw.get("log", [])
    if not isinstance(history, list) or not isinstance(log, list):
        raise OldSessionError("history/log 不是列表")
    messages = [_message(item) for item in history]
    entries: List[Tuple[str, str, str]] = []
    for item in log:
        if not isinstance(item, (list, tuple)) or not 2 <= len(item) <= 3:
            continue
        if not all(isinstance(part, str) for part in item):
            continue
        entries.append((item[0], item[1], item[2] if len(item) == 3 else ""))
    return {
        "provider_id": provider_id,
        "model_id": model_id,
        "created_at": _number(raw.get("created_at")),
        "updated_at": _number(raw.get("updated_at")) or _number(raw.get("created_at")),
        "messages": messages,
        "log": entries,
    }


def new_id_for(old_id: str, created_at: float) -> str:
    """``20251028-1052-<旧 id>``：按名排序即时间序，且可重复生成。"""
    suffix = old_id if SUFFIX_RE.match(old_id) else hashlib.sha1(old_id.encode()).hexdigest()[:6]
    return "{}-{}".format(time.strftime("%Y%m%d-%H%M", time.localtime(created_at)), suffix)


def split_turns(messages: Sequence[Message]) -> List[List[Message]]:
    """按 user 边界切轮；开头没有 user 的散消息单独成一轮。"""
    turns: List[List[Message]] = []
    for message in messages:
        if message.role == "user" or not turns:
            turns.append([])
        turns[-1].append(message)
    return turns


def turn_notes(
    turns: Sequence[Sequence[Message]], log: Sequence[Tuple[str, str, str]]
) -> Dict[int, str]:
    """把旧 log 里的 note 按任务原文从尾部对齐到轮次。"""
    notes: Dict[int, str] = {}
    index = len(turns) - 1
    for task, _reply, note in reversed(log):
        if index < 0 or turns[index][0].role != "user":
            break
        if (turns[index][0].content or "") != task:
            break
        if note:
            notes[index] = note
        index -= 1
    return notes


def _blob(text: str, blobs: Dict[str, str]) -> Tuple[str, int]:
    data = text.encode("utf-8")
    digest = hashlib.sha1(data).hexdigest()
    blobs[digest] = text
    return digest, len(data)


def build(old: Dict[str, Any], old_id: str, new_id: str):
    """返回 (事件列表, blob 表, meta)。不碰磁盘。"""
    messages: List[Message] = old["messages"]
    turns = split_turns(messages)
    notes = turn_notes(turns, old["log"])
    blobs: Dict[str, str] = {}
    specs: List[Tuple[str, Dict[str, Any]]] = [
        (
            "session_start",
            {
                "model": "{}/{}".format(old["provider_id"], old["model_id"]),
                "content": "lls {}".format(__version__),
                "note": "转换自 {}{}".format(old_id, OLD_SUFFIX),
            },
        )
    ]
    for index, turn in enumerate(turns):
        for message in turn:
            if message.role == "user":
                specs.append(("user", {"content": message.content or "", "source": "typed"}))
            elif message.role == "assistant":
                specs.append(
                    (
                        "assistant",
                        {
                            "content": message.content or "",
                            "tool_calls": tuple(message.tool_calls),
                        },
                    )
                )
            else:
                text = message.content or ""
                fields: Dict[str, Any] = {
                    "content": text,
                    "tool_call_id": message.tool_call_id or "",
                    "status": "ok",
                }
                if len(text.encode("utf-8")) > BLOB_BYTES:
                    digest, size = _blob(text, blobs)
                    fields.update(content="", blob=digest, size=size)
                specs.append(("tool", fields))
        if turn[0].role == "user":
            specs.append(("turn_end", {"status": "ok", "note": notes.get(index, "")}))

    created, updated = old["created_at"], old["updated_at"]
    total = max(1, len(specs) - 1)
    events = [
        Event(
            type=kind,
            seq=position + 1,
            ts=created + (updated - created) * position / total,
            **fields,
        )
        for position, (kind, fields) in enumerate(specs)
    ]
    first_user = next((m.content or "" for m in messages if m.role == "user"), "")
    meta = {
        "version": 1,
        "id": new_id,
        "provider_id": old["provider_id"],
        "model_id": old["model_id"],
        "title": " ".join(first_user.split())[:TITLE_CHARS],
        "cwd": "",
        "created_at": created,
        "updated_at": updated,
        "parent_id": "",
        "turns": sum(1 for event in events if event.type == "turn_end"),
        "last_seq": len(events),
        "size": 0,
    }
    return events, blobs, meta


def write_session(root: Path, new_id: str, events, blobs, meta) -> None:
    """先写到临时目录再整体改名，中途失败不会留下半条会话。"""
    staging = Path(tempfile.mkdtemp(prefix=".migrate-", dir=str(root)))
    try:
        os.chmod(str(staging), 0o700)
        for digest, text in blobs.items():
            directory = staging / BLOB_DIR
            directory.mkdir(exist_ok=True)
            os.chmod(str(directory), 0o700)
            target = directory / digest
            with open(target, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
            os.chmod(str(target), 0o600)
        payload = "".join(encode(event) + "\n" for event in events).encode("utf-8")
        path = staging / EVENTS_NAME
        with open(path, "wb") as handle:
            handle.write(payload)
        os.chmod(str(path), 0o600)
        meta["size"] = len(payload)
        with open(staging / META_NAME, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(meta, ensure_ascii=False) + "\n")
        os.chmod(str(staging / META_NAME), 0o600)
        os.replace(str(staging), str(root / new_id))
    except BaseException:
        shutil.rmtree(str(staging), ignore_errors=True)
        raise


def update_index(root: Path, mapping: Dict[str, str], dry_run: bool) -> str:
    """旧 index 的 current 指向旧 id：映射成新 id 后写回。"""
    path = root / INDEX_NAME
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(raw, dict):
        return ""
    current = raw.get("current")
    target = mapping.get(current) if isinstance(current, str) else None
    if not target:
        return ""
    if not dry_run:
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps({"version": 1, "current": target}, ensure_ascii=False) + "\n")
        os.chmod(str(tmp), 0o600)
        os.replace(str(tmp), str(path))
    return target


def find_old(root: Path) -> List[Tuple[str, Path]]:
    found: List[Tuple[str, Path]] = []
    for path in sorted(root.iterdir()):
        if not path.is_file() or path.suffix != OLD_SUFFIX:
            continue
        if OLD_ID_RE.match(path.stem):
            found.append((path.stem, path))
    return found


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把旧的 <id>.json 会话转换成事件流布局")
    parser.add_argument(
        "--root", default=None, help="会话目录（默认取配置 paths.sessions_dir）"
    )
    parser.add_argument("--dry-run", action="store_true", help="只报告，不写盘")
    options = parser.parse_args(argv)

    root = Path(options.root).expanduser() if options.root else default_sessions_dir()
    if not root.is_dir():
        print("会话目录不存在：{}".format(root))
        return 1
    old_files = find_old(root)
    if not old_files:
        print("{} 下没有旧格式会话（<id>.json）".format(root))
        return 0

    print("会话目录：{}".format(root))
    if options.dry_run:
        print("（dry-run：不写盘）")
    mapping: Dict[str, str] = {}
    failures = 0
    for old_id, path in old_files:
        try:
            old = load_old(path)
        except OldSessionError as e:
            failures += 1
            print("跳过 {}：{}".format(path.name, e))
            continue
        new_id = new_id_for(old_id, old["created_at"])
        target = root / new_id
        turns = sum(1 for m in old["messages"] if m.role == "user")
        if target.exists():
            print("跳过 {}：{} 已存在".format(old_id, new_id))
            mapping[old_id] = new_id
            continue
        events, blobs, meta = build(old, old_id, new_id)
        if not options.dry_run:
            try:
                write_session(root, new_id, events, blobs, meta)
            except OSError as e:
                failures += 1
                print("失败 {}：{}".format(old_id, e))
                continue
        mapping[old_id] = new_id
        print(
            "{}  {} 条消息 / {} 轮 / {} 个 blob  ->  {}".format(
                old_id, len(old["messages"]), turns, len(blobs), new_id
            )
        )

    current = update_index(root, mapping, options.dry_run)
    if current:
        print("当前会话指向：{}".format(current))
    if failures:
        print("有 {} 条未能转换；旧文件原样保留".format(failures))
        return 1
    print(
        "完成：{} 条；旧 <id>.json 未改动，确认无误后可自行删除".format(len(mapping))
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
