"""终端录制：原始终端 I/O 的单一事实来源。

一次启动对应一个 recording id；录制内部按大小分段。达到单次上限时丢弃
本次最早的分段，保留最新内容；关闭或分段结束时可选 gzip 压缩。
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
    """把一次终端启动写成同一 recording 下的多个分段。"""

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        recording_id: str | None = None,
        compress: bool = True,
        max_recording_bytes: int = DEFAULT_MAX_RECORDING_BYTES,
        max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
        fsync: bool = True,
    ) -> None:
        self.root = Path(root)
        self.recording_id = recording_id or time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        if recording_id is None:
            self.recording_id += "-" + secrets.token_hex(4)
        self._compress = compress
        self._max_recording_bytes = max(1024, int(max_recording_bytes))
        self._max_total_bytes = max(0, int(max_total_bytes))
        self._fsync = fsync
        self._lock = threading.RLock()
        self._closed = False
        self._frame = 0
        self._segment_no = 0
        self._segments: list[str] = []
        self._created_at = time.time()
        self.dir = self.root / self.recording_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self._recover()
        self._index = (self.dir / "index.jsonl").open("a", encoding="utf-8", newline="\n")
        self._open_segment()

    def _recover(self) -> None:
        """清理崩溃残留：临时文件、半份压缩包。"""
        for tmp in self.dir.glob("*.tmp"):
            tmp.unlink(missing_ok=True)
        for source in self.dir.glob("segment-*.cast"):
            packed = source.with_suffix(".cast.gz")
            if not packed.is_file():
                continue
            try:
                with gzip.open(packed, "rt", encoding="utf-8") as handle:
                    next(handle)
            except (OSError, EOFError):
                packed.unlink(missing_ok=True)
                continue
            source.unlink(missing_ok=True)

    # ---- 分段 ----

    def _segment_name(self, no: int) -> str:
        return "segment-{:06d}.cast".format(no)

    def _segment_path(self, name: str) -> Path:
        return self.dir / name

    def _open_segment(self) -> None:
        self._segment_no += 1
        name = self._segment_name(self._segment_no)
        self._stream_name = name
        self._stream = (self.dir / name).open("a", encoding="utf-8", newline="\n")
        self._started = time.monotonic()
        self._segments.append(name)
        self._append_line(
            self._stream,
            {
                "version": 1,
                "id": self.recording_id,
                "segment": name,
                "created_at": self._created_at,
            },
        )
        self._write_meta()

    def _close_segment(self) -> None:
        if self._stream is None:
            return
        self._stream.flush()
        if self._fsync:
            os.fsync(self._stream.fileno())
        self._stream.close()
        self._stream = None
        path = self._segment_path(self._stream_name)
        final = path
        if self._compress:
            final = path.with_suffix(".cast.gz")
            temp = final.with_name(final.name + ".tmp")
            with path.open(encoding="utf-8") as reader, gzip.open(
                temp, "wt", encoding="utf-8", compresslevel=6
            ) as writer:
                shutil.copyfileobj(reader, writer)
            os.replace(temp, final)
            path.unlink(missing_ok=True)
            self._segments = [
                final.name if item == self._stream_name else item for item in self._segments
            ]
        self._write_meta()

    def _drop_segment(self, name: str) -> None:
        path = self._segment_path(name)
        self._segments.remove(name)
        self._index.flush()
        self._index.close()
        kept = []
        for line in (self.dir / "index.jsonl").read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if item.get("segment") != name:
                kept.append(line)
        index = self.dir / "index.jsonl"
        temp = index.with_name(index.name + ".tmp")
        temp.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
        os.replace(temp, index)
        self._index = index.open("a", encoding="utf-8", newline="\n")
        path.unlink(missing_ok=True)
        self._write_meta()

    def _segment_bytes(self) -> int:
        return sum(
            self._segment_path(name).stat().st_size
            for name in self._segments
            if self._segment_path(name).is_file()
        )

    def _prune_segments(self) -> None:
        while len(self._segments) > 1 and self._segment_bytes() > self._max_recording_bytes:
            self._drop_segment(self._segments[0])

    def _rotate(self) -> None:
        self._close_segment()
        self._prune_segments()
        self._open_segment()

    # ---- 元数据 ----

    def _write_meta(self) -> None:
        temp = self.dir / "meta.json.tmp"
        temp.write_text(
            json.dumps(
                {
                    "version": 1,
                    "id": self.recording_id,
                    "created_at": self._created_at,
                    "compressed": self._compress,
                    "frames": self._frame,
                    "retained_from": self._retained_from(),
                    "segments": list(self._segments),
                    "bytes": self._segment_bytes(),
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        os.replace(temp, self.dir / "meta.json")

    def _retained_from(self) -> int:
        first = None
        for line in (self.dir / "index.jsonl").read_text(encoding="utf-8").splitlines():
            try:
                frame = int(json.loads(line)["frame"])
            except (ValueError, KeyError):
                continue
            first = frame if first is None else min(first, frame)
        return first or 1

    # ---- 写入 ----

    @property
    def current_ref(self) -> TerminalRef:
        with self._lock:
            return TerminalRef(self.recording_id, self._frame)

    def record(self, channel: str, data: bytes | str) -> TerminalRef:
        """写入一帧；大块输入按单次上限切帧。"""
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
                "segment": self._stream_name,
                "time": event[0],
                "offset": offset,
                "channel": channel,
                "bytes": len(payload),
            },
        )
        self._stream.flush()
        self._index.flush()
        if self._fsync:
            os.fsync(self._stream.fileno())
            os.fsync(self._index.fileno())
        return TerminalRef(self.recording_id, self._frame)

    # ---- 生命周期 ----

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._close_segment()
            self._prune_segments()
            self._index.close()
            self._prune_root()

    def _prune_root(self) -> None:
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
            self._tombstone(path.name)
            shutil.rmtree(path, ignore_errors=True)
            total -= size

    def _tombstone(self, recording_id: str) -> None:
        path = self.root / "tombstones.jsonl"
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(
                json.dumps(
                    {"recording_id": recording_id, "deleted_at": time.time()},
                    ensure_ascii=False,
                )
                + "\n"
            )
            handle.flush()
            os.fsync(handle.fileno())

    # ---- 读取 ----

    def iter_events(self) -> Iterator[dict]:
        """按分段顺序读回事件，兼容未压缩与 gzip。"""
        for name in list(self._segments):
            path = self._segment_path(name)
            if not path.is_file():
                continue
            if path.suffix == ".gz":
                handle_cm = gzip.open(path, "rt", encoding="utf-8")
            else:
                handle_cm = path.open(encoding="utf-8")
            with handle_cm as handle:
                for line in handle:
                    try:
                        item = json.loads(line)
                    except ValueError:
                        continue
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
