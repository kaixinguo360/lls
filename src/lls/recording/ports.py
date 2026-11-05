"""引擎与会话层只需要的两个窄接口。

写端只接收「一帧字节」；会话层只记录「当前帧号」。两边都不 import 具体实现，
换成内存录制或远端录制时只需对上这两个形状。
"""

from __future__ import annotations

from typing import Protocol

__all__ = ["TerminalCursor", "TerminalSink"]


class TerminalSink(Protocol):
    """能接收终端字节的地方。

    通道只有 ``i``（写子进程）、``o``（读子进程）与 ``r``（终端 resize，
    载荷为 ``"{COLS}x{ROWS}"``）。录制是可选旁路，写失败由调用方就地兜住。
    """

    def record(self, channel: str, data: bytes | str) -> object:
        """写入一帧，返回当前时刻的引用。"""
        ...


class TerminalCursor(Protocol):
    """一条事件要记的录制位置：录制 id 加单调递增的帧号。"""

    recording: str
    frame: int
