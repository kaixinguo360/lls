"""终端录制：原始终端 I/O 的单一事实来源。

录制格式接近 asciinema：时间、通道、原始字节。索引把 ``recording:frame``
映射到未压缩流里的字节位置；关闭录制时可选 gzip，减少长期占用。
"""

from __future__ import annotations

import base64
import gzip
import json
import os
import secrets
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

__all__ = [
    "DEFAULT_MAX_RECORDING_BYTES",
    "DEFAULT_MAX_TOTAL_BYTES",
    "TerminalRecorder",
    "TerminalRef",
    "parse_ref",
]

DEFAULT_MAX_RECORDING_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_TOTAL_BYTES = 1024 * 1024 * 1024


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
    """把终端 I/O 写成可滚动、可压缩的 ``stream.cast`` + ``index.jsonl``。"""

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        recording_id: str | None = None,
        compress: bool = True,
        max_recording_bytes: int = DEFAULT_MAX_RECORDING_BYTES,
        max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    ) -> None:
        self.root = Path(root)
        self._base_id = recording_id or time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        if recording_id is None:
            self._base_id += "-" + secrets.token_hex(4)
        self._compress = compress
        self._max_recording_bytes = max(1024, int(max_recording_bytes))
        self._max_total_bytes = max(0, int(max_total_bytes))
        self._lock = threading.RLock()
        self._closed = False
        self._part = 0
        self.recording_id = ""
        self.dir: Path
        self._frame = 0
        self._started = 0.0
        self._stream = None
        self._index = None
        self._open_part()

    # ---- 生命周期 ----

    def _open_part(self) -> None:
        self._part += 1
        self.recording_id = (
            self._base_id if self._part == 1 else "{}-{}".format(self._base_id, self._part)
        )
        self.dir = self.root / self.recording_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self._stream = (self.dir / "stream.cast").open("a", encoding="utf-8", newline="\n")
        self._index = (self.dir / "index.jsonl").open("a", encoding="utf-8", newline="\n")
        self._frame = 0
        self._started = time.monotonic()
        created = time.time()
        self._write_meta({"version": 1, "id": self.recording_id, "created_at": created})
        self._append_line(
            self._stream,
            {"version": 1, "id": self.recording_id, "created_at": created},
        )

    def _write_meta(self, extra: dict) -> None:
        (self.dir / "meta.json").write_text(
            json.dumps(extra, ensure_ascii=False) + "\n", encoding="utf-8"
        )

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._finish_part()
            self._prune()

    def _finish_part(self) -> None:
        if self._stream is None or self._index is None:
            return
        size = self._stream.tell()
        self._stream.close()
        self._index.close()
        self._stream = None
        self._index = None
        if self._compress:
            source = self.dir / "stream.cast"
            target = self.dir / "stream.cast.gz"
            with source.open(encoding="utf-8") as reader, gzip.open(
                target, "wt", encoding="utf-8", compresslevel=6
            ) as writer:
                shutil.copyfileobj(reader, writer)
            source.unlink()
        self._write_meta(
            {
                "version": 1,
                "id": self.recording_id,
                "created_at": self._created_at(),
                "bytes": size,
                "compressed": self._compress,
                "frames": self._frame,
            }
        )

    def _created_at(self) -> float:
        try:
            return float(
                json.loads((self.dir / "meta.json").read_text(encoding="utf-8"))["created_at"]
            )
        except (OSError, ValueError, KeyError):
            return time.time()

    def _rotate(self) -> None:
        self._finish_part()
        self._prune()
        self._open_part()

    def _prune(self) -> None:
        if self._max_total_bytes <= 0:
            return
        entries = []
        for path in self.root.iterdir():
            if not path.is_dir():
                continue
            try:
                meta = json.loads((path / "meta.json").read_text(encoding="utf-8"))
                created = float(meta["created_at"])
            except (OSError, ValueError, KeyError):
                continue
            size = sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
            entries.append((created, size, path))
        total = sum(item[1] for item in entries)
        for _, size, path in sorted(entries):
            if total <= self._max_total_bytes:
                break
            if path == self.dir:
                continue
            shutil.rmtree(path, ignore_errors=True)
            total -= size

    # ---- 写入 ----

    @property
    def current_ref(self) -> TerminalRef:
        with self._lock:
            return TerminalRef(self.recording_id, self._frame)

    def record(self, channel: str, data: bytes | str) -> TerminalRef:
        """写入一帧；大块输入按单次上限切帧，避免单帧突破上限。"""
        if channel not in ("i", "o"):
            raise ValueError("channel must be 'i' or 'o'")
        payload = data.encode("utf-8") if isinstance(data, str) else bytes(data)
        with self._lock:
            if self._closed:
                raise RuntimeError("recorder is closed")
            if not payload:
                return self.current_ref
            chunk_size = max(1, self._max_recording_bytes // 2)
            ref = self.current_ref
            for start in range(0, len(payload), chunk_size):
                chunk = payload[start : start + chunk_size]
                if self._stream.tell() and self._stream.tell() + len(chunk) > self._max_recording_bytes:
                    self._rotate()
                ref = self._write_frame(channel, chunk)
            return ref

    def _write_frame(self, channel: str, payload: bytes) -> TerminalRef:
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

    # ---- 读取 ----

    def iter_events(self) -> Iterator[dict]:
        """按顺序读回当前分段，兼容未压缩与 gzip。"""
        plain = self.dir / "stream.cast"
        packed = self.dir / "stream.cast.gz"
        if packed.is_file():
            handle_cm = gzip.open(packed, "rt", encoding="utf-8")
        else:
            handle_cm = plain.open(encoding="utf-8")
        with handle_cm as handle:
            for line in handle:
                item = json.loads(line)
                if isinstance(item, dict):
                    continue
                yield {
                    "time": item[0],
                    "channel": item[1],
                    "data": base64.b64decode(item[2]),
                }

    @staticmethod
    def _append_line(handle, value: object) -> None:
        handle.write(json.dumps(value, ensure_ascii=False) + "\n")
