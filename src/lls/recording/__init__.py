"""终端录制与回放。

这个包是自包含的：核心读写只依赖标准库，``replay/`` 额外只依赖 pyte；
包内不 import 任何 ``lls.*``。LLS 只通过三个窄接缝使用它：写端接收引擎的
字节，会话层记录 :class:`TerminalRef`，CLI 把 ``recording`` 挂成子命令。

公开入口：

* :class:`TerminalRecorder` —— 写端；
* :class:`Recording` —— 读端，产出带绝对时间的 :class:`Frame`；
* :class:`TerminalRef` / :func:`parse_ref` —— 会话事件里的终端时刻引用。
"""

from __future__ import annotations

from .errors import (
    FrameUnavailable,
    RecordingCorrupt,
    RecordingDeleted,
    RecordingError,
    RecordingNotFound,
)
from .format import (
    CHANNEL_INPUT,
    CHANNEL_OUTPUT,
    CHANNEL_RESIZE,
    CHANNELS,
    FORMAT_VERSION,
)
from .model import Frame, RecordingMeta, TerminalRef, parse_ref
from .ports import TerminalCursor, TerminalSink
from .reader import Recording, tombstone_time
from .writer import (
    DEFAULT_MAX_RECORDING_BYTES,
    DEFAULT_MAX_TOTAL_BYTES,
    TerminalRecorder,
)

__all__ = [
    "CHANNEL_INPUT",
    "CHANNEL_OUTPUT",
    "CHANNEL_RESIZE",
    "CHANNELS",
    "DEFAULT_MAX_RECORDING_BYTES",
    "DEFAULT_MAX_TOTAL_BYTES",
    "FORMAT_VERSION",
    "Frame",
    "FrameUnavailable",
    "Recording",
    "RecordingCorrupt",
    "RecordingDeleted",
    "RecordingError",
    "RecordingMeta",
    "RecordingNotFound",
    "TerminalCursor",
    "TerminalRecorder",
    "TerminalRef",
    "TerminalSink",
    "parse_ref",
    "tombstone_time",
]
