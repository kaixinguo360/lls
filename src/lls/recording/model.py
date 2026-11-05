"""录制层的数据模型：帧、元数据与终端时刻引用。

这些类型不依赖 pyte，也不认识 lls 的会话或界面；读取端和播放端都只从这里
拿形状。
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["Frame", "RecordingMeta", "TerminalRef", "parse_ref"]


@dataclass(frozen=True)
class TerminalRef:
    """一个可持久化的终端时刻引用。"""

    recording: str
    frame: int

    def __str__(self) -> str:
        return f"{self.recording}:{self.frame}"


def parse_ref(value: str) -> TerminalRef:
    recording, _, raw = value.rpartition(":")
    if not recording or not raw.isdigit():
        raise ValueError(f"bad terminal ref: {value!r}")
    return TerminalRef(recording, int(raw))


@dataclass(frozen=True)
class Frame:
    """一帧录制：全局帧号、绝对时间、通道与原始字节。"""

    number: int
    at: float
    channel: str
    data: bytes


@dataclass(frozen=True)
class RecordingMeta:
    """``meta.json`` 的内容；``rows``/``columns`` 在录制时尺寸未知可为空。"""

    id: str
    created_at: float
    closed_at: float | None
    rows: int | None
    columns: int | None
    compressed: bool
    frames: int
    duration: float
    retained_from: int
    segments: tuple[str, ...]
    bytes: int

    @property
    def geometry(self) -> tuple[int, int] | None:
        """``(rows, columns)``；录制时不知道尺寸就是 ``None``。"""
        if self.rows is None or self.columns is None:
            return None
        return self.rows, self.columns
