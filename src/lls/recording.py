"""终端录制：原始终端 I/O 的单一事实来源。

录制格式刻意接近 asciinema：时间、通道、原始字节；另外单独写一份索引，
让 ``recording:frame`` 能随机定位，而不必从头解析整段会话。
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

__all__ = ["TerminalRecorder", "TerminalRef", "parse_ref"]


@dataclass(frozen=True)
class TerminalRef:
    """一个可持久化的终端时刻引用。"""

    recording: str
    frame: int

    def __str__(self) -> str:
        return "{}:{}".format(self.recording, self.frame)


def parse_ref(value: str) -> TerminalRef:
    recording, _, raw = value.rpartition(":")
    if not recording or not raw.isdigit():
        raise ValueError("bad terminal ref: {!r}".format(value))
    return TerminalRef(recording, int(raw))


class TerminalRecorder:
    """把一次终端会话写成 ``stream.cast`` + ``index.jsonl``。"""

    def __init__(self, root: str | os.PathLike[str], *, recording_id: str | None = None) -> None:
        self.root = Path(root)
        self.recording_id = recording_id or time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        if recording_id is None:
            self.recording_id += "-" + secrets.token_hex(4)
        self.dir = self.root / self.recording_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self._stream = (self.dir / "stream.cast").open("a", encoding="utf-8", newline="\n")
        self._index = (self.dir / "index.jsonl").open("a", encoding="utf-8", newline="\n")
        self._meta = self.dir / "meta.json"
        self._lock = threading.Lock()
        self._frame = 0
        self._started = time.monotonic()
        self._closed = False
        self._meta.write_text(
            json.dumps(
                {
                    "version": 1,
                    "id": self.recording_id,
                    "created_at": time.time(),
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        self._append_line(
            self._stream,
            {"version": 1, "id": self.recording_id, "created_at": time.time()},
        )

    def record(self, channel: str, data: bytes | str) -> TerminalRef:
        """写入一帧；``channel`` 只用 ``i``（输入）和 ``o``（输出）。"""
        if channel not in ("i", "o"):
            raise ValueError("channel must be 'i' or 'o'")
        payload = data.encode("utf-8") if isinstance(data, str) else bytes(data)
        with self._lock:
            if self._closed:
                raise RuntimeError("recorder is closed")
            offset = self._stream.tell()
            now = time.monotonic() - self._started
            self._frame += 1
            event = [round(now, 6), channel, base64.b64encode(payload).decode("ascii")]
            self._append_line(self._stream, event)
            self._append_line(
                self._index,
                {
                    "frame": self._frame,
                    "time": event[0],
                    "offset": offset,
                    "channel": channel,
                    "bytes": len(payload),
                },
            )
            self._stream.flush()
            self._index.flush()
            return TerminalRef(self.recording_id, self._frame)

    def iter_events(self) -> Iterator[dict]:
        """按顺序读回事件，供离线训练使用。"""
        with (self.dir / "stream.cast").open(encoding="utf-8") as handle:
            for line in handle:
                item = json.loads(line)
                if isinstance(item, dict):
                    continue
                yield {
                    "time": item[0],
                    "channel": item[1],
                    "data": base64.b64decode(item[2]),
                }

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._stream.close()
            self._index.close()

    def __enter__(self) -> "TerminalRecorder":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @staticmethod
    def _append_line(handle, value: object) -> None:
        handle.write(json.dumps(value, ensure_ascii=False) + "\n")
