"""agent 会话的存储：进程内缓存 + 磁盘落盘。

会话是**独立实体**：它有自己的 id，provider/model、历史与日志都只是它的
字段。这样同一个模型可以并行多条会话，换模型也不必丢掉上下文。

本模块只管「按 id 存取」与「一个会话一个文件」的落盘，不认识终端、不认识
命令循环。磁盘布局（``$LLS_SESSIONS_HOME/sessions``，默认
``~/.local/lls/sessions``）::

    <id>.json    一条会话：provider/model、history、log、时间戳
    index.json   当前会话指针

写入走「临时文件 + ``os.replace``」，读时坏文件直接跳过：落盘是便利，不能
因为一个坏文件让 ``agent`` 起不来。写失败只记在 ``last_error`` 里，不把异常
抛给刚完成一轮的会话。
"""

from __future__ import annotations

import builtins
import contextlib
import json
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Tuple, Union

from lls.ai import Message, ToolCall

__all__ = [
    "MAX_SESSIONS",
    "SCHEMA_VERSION",
    "SessionState",
    "SessionStore",
    "default_sessions_dir",
    "new_session_id",
    "store",
]

# 最多保留几条会话；超量按「最近使用」淘汰（内存与磁盘一起）。
MAX_SESSIONS = 8

# 会话文件的格式版本；版本不对就跳过（宁可当没有，也不要读错结构）。
SCHEMA_VERSION = 1

INDEX_NAME = "index.json"
SESSION_SUFFIX = ".json"
_TMP_SUFFIX = ".tmp"

# 会话 id 会直接做文件名：只接受生成器产生的那一类，杜绝路径穿越。
_ID_RE = re.compile(r"^[0-9a-f]{4,64}$")

# 会话提示里记录的日志条目：(任务, 答复, 备注)
LogEntry = Tuple[str, str, str]

PathLike = Union[str, os.PathLike]


def new_session_id() -> str:
    """生成一个短而可读的会话 id（3 字节随机 → 6 个十六进制字符）。"""
    return secrets.token_hex(3)


def default_sessions_dir() -> Path:
    """会话目录：``$LLS_SESSIONS_HOME`` > ``~/.local``；会话在它下面的 ``sessions``。

    与配置的 ``~/.config/lls`` 对称：路径短，一层就够。
    """
    base = os.environ.get("LLS_SESSIONS_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".local" / "lls"
    return root / "sessions"


@dataclass
class SessionState:
    """一条会话的全部可持久状态。

    ``history`` 是 :class:`~lls.ai.Message` 序列（不含 system），可以直接作为
    下一轮 ``Agent.run`` 的 history；``log`` 只用于重建会话提示。
    """

    id: str = ""
    provider_id: str = ""
    model_id: str = ""
    history: list[Message] = field(default_factory=list)
    log: list[LogEntry] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    @property
    def model(self) -> str:
        """``provider/model`` 形式，用于展示。"""
        return f"{self.provider_id}/{self.model_id}"

    @property
    def turns(self) -> int:
        """已完成的轮数（按历史里的 user 消息数）。"""
        return sum(1 for message in self.history if message.role == "user")


class SessionStore:
    """按 id 存取会话；另有「当前会话」指针供 ``agent`` 无参进入。

    ``root`` 为 None 时只在内存里存（单元测试、一次性调用用得上）；给了目录
    就每次变更都落盘，并在构造时把已有会话读回来。
    """

    def __init__(
        self, keep: int = MAX_SESSIONS, root: PathLike | None = None
    ) -> None:
        self._sessions: dict[str, SessionState] = {}
        # 「最近使用」的次序。时间戳在同一 tick 内可能并列，只按它排序会让
        # 淘汰逻辑删掉刚建的那条，因此再记一个单调序号兜底。
        self._order: dict[str, int] = {}
        self._counter = 0
        self._current: str | None = None
        self._keep = max(1, int(keep))
        self._root = Path(root).expanduser() if root is not None else None
        # 最近一次落盘失败的原因（不影响会话本身）；命令层可用来提示。
        self.last_error: str | None = None
        if self._root is not None:
            self._load()

    @property
    def root(self) -> Path | None:
        """落盘目录；``None`` 表示纯内存。"""
        return self._root

    # ---- 创建与查询 ----

    def new(
        self, provider_id: str, model_id: str, *, session_id: str = ""
    ) -> SessionState:
        """新建一条会话并设为当前会话。"""
        state = SessionState(
            id=session_id or self._unused_id(),
            provider_id=provider_id,
            model_id=model_id,
        )
        if not _ID_RE.match(state.id):
            raise ValueError(f"bad session id: {state.id!r}")
        self._sessions[state.id] = state
        self._current = state.id
        self._touch(state.id)
        self.save(state)
        return state

    def get(self, session_id: str) -> SessionState | None:
        return self._sessions.get(session_id)

    def list(self) -> builtins.list[SessionState]:
        """全部会话，最近使用的在前。

        时间戳并列时按登记次序决出先后，保证排序与淘汰都是确定的。
        """
        return sorted(
            self._sessions.values(),
            key=lambda state: (state.updated_at, self._order.get(state.id, 0)),
            reverse=True,
        )

    def __len__(self) -> int:
        return len(self._sessions)

    # ---- 落盘 ----

    def save(self, state: SessionState) -> None:
        """把一条会话的当前内容写到磁盘（内存里没有就先登记）。

        调用方负责在写之前更新 ``updated_at``；这里只做序列化与落盘。写失败
        记录进 ``last_error``，不抛异常 —— 一轮任务已经完成，不该因为磁盘满
        或权限问题在终端里变成报错。
        """
        if self._sessions.get(state.id) is not state:
            self._sessions[state.id] = state
        self._touch(state.id)
        self._write_state(state)
        self._write_index()
        self.trim()

    # ---- 当前会话 ----

    def current(self) -> SessionState | None:
        if self._current is None:
            return None
        return self._sessions.get(self._current)

    def set_current(self, session_id: str) -> None:
        if session_id in self._sessions:
            self._current = session_id
            self._write_index()

    # ---- 删除与淘汰 ----

    def drop(self, session_id: str) -> bool:
        state = self._sessions.pop(session_id, None)
        if state is None:
            return False
        if self._current == session_id:
            self._current = None
        self._order.pop(session_id, None)
        self._unlink(session_id)
        self._write_index()
        return True

    def _touch(self, session_id: str) -> None:
        self._counter += 1
        self._order[session_id] = self._counter

    def trim(self) -> None:
        """超出上限时丢掉最久未用的会话（磁盘上的文件一起删）。"""
        if len(self._sessions) <= self._keep:
            return
        for state in self.list()[self._keep:]:
            self.drop(state.id)

    # ---- 内部：读写 ----

    def _load(self) -> None:
        root = self._root
        if root is None:
            return
        try:
            entries = sorted(root.iterdir())
        except FileNotFoundError:
            return
        except OSError as e:
            self.last_error = f"{root}: {e}"
            return
        for path in entries:
            if path.suffix != SESSION_SUFFIX or path.name == INDEX_NAME:
                continue
            session_id = path.name[: -len(SESSION_SUFFIX)]
            if not _ID_RE.match(session_id):
                continue
            state = self._read_state(path, session_id)
            if state is not None:
                self._sessions[state.id] = state
        self._read_index()
        self.trim()

    def _read_state(self, path: Path, session_id: str) -> SessionState | None:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        try:
            return _decode_state(raw, session_id)
        except (TypeError, ValueError, KeyError):
            return None

    def _read_index(self) -> None:
        root = self._root
        if root is None:
            return
        try:
            raw = json.loads((root / INDEX_NAME).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        current = raw.get("current")
        if isinstance(current, str) and current in self._sessions:
            self._current = current

    def _write_state(self, state: SessionState) -> None:
        root = self._root
        if root is None or not _ID_RE.match(state.id):
            return
        self._atomic_write(root / (state.id + SESSION_SUFFIX), _encode_state(state))

    def _write_index(self) -> None:
        root = self._root
        if root is None:
            return
        self._atomic_write(
            root / INDEX_NAME,
            {"version": SCHEMA_VERSION, "current": self._current},
        )

    def _unlink(self, session_id: str) -> None:
        root = self._root
        if root is None or not _ID_RE.match(session_id):
            return
        try:
            (root / (session_id + SESSION_SUFFIX)).unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            self.last_error = f"{root}: {e}"

    def _atomic_write(self, path: Path, payload: dict[str, Any]) -> None:
        """临时文件 + ``os.replace``；失败只记录，不打断会话。"""
        root = self._root
        if root is None:
            return
        tmp = path.with_name(path.name + _TMP_SUFFIX)
        try:
            root.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(OSError):
                os.chmod(str(root), 0o700)
            data = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
            with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(data)
            with contextlib.suppress(OSError):
                os.chmod(str(tmp), 0o600)
            os.replace(str(tmp), str(path))
        except OSError as e:
            self.last_error = f"{path}: {e}"
            with contextlib.suppress(OSError):
                tmp.unlink()

    # ---- 内部：id ----

    def _unused_id(self) -> str:
        session_id = new_session_id()
        while session_id in self._sessions:
            session_id = new_session_id()
        return session_id


# ---- 序列化 ----

def _encode_state(state: SessionState) -> dict[str, Any]:
    return {
        "version": SCHEMA_VERSION,
        "id": state.id,
        "provider_id": state.provider_id,
        "model_id": state.model_id,
        "created_at": state.created_at,
        "updated_at": state.updated_at,
        "history": [_encode_message(message) for message in state.history],
        "log": [list(entry) for entry in state.log],
    }


def _encode_message(message: Message) -> dict[str, Any]:
    payload: dict[str, Any] = {"role": message.role}
    if message.content is not None:
        payload["content"] = message.content
    if message.tool_calls:
        payload["tool_calls"] = [
            {"id": call.id, "name": call.name, "arguments": call.arguments}
            for call in message.tool_calls
        ]
    if message.tool_call_id is not None:
        payload["tool_call_id"] = message.tool_call_id
    return payload


def _decode_state(raw: Any, session_id: str) -> SessionState:
    if not isinstance(raw, dict):
        raise TypeError("session is not an object")
    if raw.get("version") != SCHEMA_VERSION:
        raise ValueError("unsupported session version: {!r}".format(raw.get("version")))
    provider_id = raw.get("provider_id")
    model_id = raw.get("model_id")
    if not isinstance(provider_id, str) or not isinstance(model_id, str):
        raise TypeError("session has no provider/model")
    history = raw.get("history", [])
    if not isinstance(history, list):
        raise TypeError("history is not a list")
    log = raw.get("log", [])
    if not isinstance(log, list):
        raise TypeError("log is not a list")
    return SessionState(
        id=session_id,
        provider_id=provider_id,
        model_id=model_id,
        history=[_decode_message(item) for item in history],
        log=[_decode_log_entry(item) for item in log],
        created_at=_number(raw.get("created_at")),
        updated_at=_number(raw.get("updated_at")),
    )


def _decode_message(raw: Any) -> Message:
    if not isinstance(raw, dict):
        raise TypeError("message is not an object")
    role = raw.get("role")
    if not isinstance(role, str) or not role:
        raise ValueError("message has no role")
    content = raw.get("content")
    if content is not None and not isinstance(content, str):
        raise TypeError("message content is not a string")
    calls_raw = raw.get("tool_calls", [])
    if not isinstance(calls_raw, list):
        raise TypeError("tool_calls is not a list")
    call_id = raw.get("tool_call_id")
    if call_id is not None and not isinstance(call_id, str):
        raise TypeError("tool_call_id is not a string")
    return Message(
        role=role,
        content=content,
        tool_calls=tuple(_decode_tool_call(item) for item in calls_raw),
        tool_call_id=call_id,
    )


def _decode_tool_call(raw: Any) -> ToolCall:
    if not isinstance(raw, dict):
        raise TypeError("tool call is not an object")
    call_id = raw.get("id")
    name = raw.get("name")
    arguments = raw.get("arguments", "")
    if not isinstance(call_id, str) or not isinstance(name, str):
        raise TypeError("tool call has no id/name")
    if not isinstance(arguments, str):
        raise TypeError("tool call arguments is not a string")
    return ToolCall(id=call_id, name=name, arguments=arguments)


def _decode_log_entry(raw: Any) -> LogEntry:
    if not isinstance(raw, (list, tuple)) or not 2 <= len(raw) <= 3:
        raise TypeError("log entry is not a 2/3-item list")
    if not all(isinstance(item, str) for item in raw):
        raise TypeError("log entry is not all strings")
    return raw[0], raw[1], raw[2] if len(raw) == 3 else ""


def _number(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    return 0.0


_STORE: SessionStore | None = None


def store() -> SessionStore:
    """进程内单例：跨多次 ``agent`` 调用共享同一份会话，并落盘续命。"""
    global _STORE
    if _STORE is None:
        _STORE = SessionStore(root=default_sessions_dir())
    return _STORE
