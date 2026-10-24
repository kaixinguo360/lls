"""agent 会话的进程内存储。

会话是**独立实体**：它有自己的 id，provider/model、历史与日志都只是它的
字段。这样同一个模型可以并行多条会话，换模型也不必丢掉上下文，将来落盘时
一个会话一个文件即可。

本模块只管「按 id 存取」，不认识终端、不认识命令循环；生命周期与 lls 进程
一致（退出进程即丢失）。
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from lls.ai import Message

__all__ = ["MAX_SESSIONS", "SessionState", "SessionStore", "new_session_id", "store"]

# 进程内最多保留几条会话；超量按「最近使用」淘汰。
MAX_SESSIONS = 8

# 会话提示里记录的日志条目：(任务, 答复, 备注)
LogEntry = Tuple[str, str, str]


def new_session_id() -> str:
    """生成一个短而可读的会话 id（6 字节随机 → 12 个十六进制字符）。"""
    return secrets.token_hex(3)


@dataclass
class SessionState:
    """一条会话的全部可持久状态。

    ``history`` 是 :class:`~lls.ai.Message` 序列（不含 system），可以直接作为
    下一轮 ``Agent.run`` 的 history；``log`` 只用于重建会话提示。
    """

    id: str = ""
    provider_id: str = ""
    model_id: str = ""
    history: List[Message] = field(default_factory=list)
    log: List[LogEntry] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    @property
    def model(self) -> str:
        """``provider/model`` 形式，用于展示。"""
        return "%s/%s" % (self.provider_id, self.model_id)

    @property
    def turns(self) -> int:
        """已完成的轮数（按历史里的 user 消息数）。"""
        return sum(1 for message in self.history if message.role == "user")


class SessionStore:
    """按 id 存取会话；另有「当前会话」指针供 ``agent`` 无参进入。"""

    def __init__(self, keep: int = MAX_SESSIONS) -> None:
        self._sessions: Dict[str, SessionState] = {}
        self._current: Optional[str] = None
        self._keep = max(1, int(keep))

    # ---- 创建与查询 ----

    def new(self, provider_id: str, model_id: str, *, session_id: str = "") -> SessionState:
        """新建一条会话并设为当前会话。"""
        state = SessionState(
            id=session_id or self._unused_id(),
            provider_id=provider_id,
            model_id=model_id,
        )
        self._sessions[state.id] = state
        self._current = state.id
        self.trim()
        return state

    def get(self, session_id: str) -> Optional[SessionState]:
        return self._sessions.get(session_id)

    def list(self) -> List[SessionState]:
        """全部会话，最近使用的在前。"""
        return sorted(
            self._sessions.values(), key=lambda state: state.updated_at, reverse=True
        )

    def __len__(self) -> int:
        return len(self._sessions)

    # ---- 当前会话 ----

    def current(self) -> Optional[SessionState]:
        if self._current is None:
            return None
        return self._sessions.get(self._current)

    def set_current(self, session_id: str) -> None:
        if session_id in self._sessions:
            self._current = session_id

    # ---- 删除与淘汰 ----

    def drop(self, session_id: str) -> bool:
        state = self._sessions.pop(session_id, None)
        if state is None:
            return False
        if self._current == session_id:
            self._current = None
        return True

    def trim(self) -> None:
        """超出上限时丢掉最久未用的会话。"""
        if len(self._sessions) <= self._keep:
            return
        for state in self.list()[self._keep:]:
            self.drop(state.id)

    # ---- 内部 ----

    def _unused_id(self) -> str:
        session_id = new_session_id()
        while session_id in self._sessions:
            session_id = new_session_id()
        return session_id


_STORE: Optional[SessionStore] = None


def store() -> SessionStore:
    """进程内单例：跨多次 ``agent`` 调用共享同一份会话。"""
    global _STORE
    if _STORE is None:
        _STORE = SessionStore()
    return _STORE
