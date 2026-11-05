"""导出 asciicast v2：录制容器 → 标准单文件格式。

``o`` 事件按增量 UTF-8 解码，避免一帧刚好切断多字节字符时导出乱码；``i``
默认不导出，因为录制里可能含密码，且 asciinema 播放器主要呈现输出。
"""

from __future__ import annotations

import codecs
import json
from typing import TextIO

from ..errors import RecordingError
from ..format import CHANNEL_INPUT, CHANNEL_OUTPUT, CHANNEL_RESIZE
from ..reader import Recording

__all__ = ["export_asciicast"]


def export_asciicast(
    recording: Recording,
    out: TextIO,
    *,
    with_input: bool = False,
    size: tuple[int, int] | None = None,
) -> None:
    geometry = size or recording.geometry
    if geometry is None:
        raise RecordingError("录制没有尺寸，请用 --size 指定")
    rows, columns = geometry
    duration = recording.duration()
    header = {
        "version": 2,
        "width": columns,
        "height": rows,
        "timestamp": int(recording.meta.created_at),
        "duration": round(duration, 6),
    }
    out.write(json.dumps(header, ensure_ascii=False) + "\n")

    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    last_at = 0.0
    for frame in recording.frames():
        last_at = frame.at
        if frame.channel == CHANNEL_OUTPUT:
            text = decoder.decode(frame.data)
            if text:
                _write(out, frame.at, CHANNEL_OUTPUT, text)
        elif frame.channel == CHANNEL_INPUT:
            if with_input:
                _write(out, frame.at, CHANNEL_INPUT, frame.data.decode("utf-8", "replace"))
        elif frame.channel == CHANNEL_RESIZE:
            _write(out, frame.at, CHANNEL_RESIZE, frame.data.decode("ascii", "replace"))
    trailing = decoder.decode(b"", final=True)
    if trailing:
        _write(out, last_at, CHANNEL_OUTPUT, trailing)


def _write(out: TextIO, at: float, channel: str, text: str) -> None:
    out.write(json.dumps([round(at, 6), channel, text], ensure_ascii=False) + "\n")
