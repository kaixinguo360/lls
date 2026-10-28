"""会话：事件流落盘、上下文窗口、按 id 存取。

对外只暴露这一层：``SessionStore`` 开/建/列/删/叉/导出，``Session`` 追加
事件并派生消息。窗口裁剪在 :mod:`.window`，事件模型在 :mod:`.events`。
"""

from .events import Event, fold, log_entries, task_message
from .store import (
    MAX_SESSIONS,
    Session,
    SessionBusy,
    SessionError,
    SessionMeta,
    SessionStore,
    default_sessions_dir,
    new_session_id,
    store,
)
from .window import (
    COMPACT_TARGET_RATIO,
    COMPACT_TRIGGER_RATIO,
    Window,
    default_budget,
    estimate_tokens,
    fit,
    last_turns,
)

__all__ = [
    "COMPACT_TARGET_RATIO",
    "COMPACT_TRIGGER_RATIO",
    "Event",
    "MAX_SESSIONS",
    "Session",
    "SessionBusy",
    "SessionError",
    "SessionMeta",
    "SessionStore",
    "Window",
    "default_budget",
    "default_sessions_dir",
    "estimate_tokens",
    "fit",
    "fold",
    "last_turns",
    "log_entries",
    "new_session_id",
    "store",
    "task_message",
]
