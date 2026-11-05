"""终端录制写端：原始终端 I/O 的单一事实来源。

一次启动对应一个 recording id；录制内部按大小分段。达到单次上限时丢弃本次
最早的分段，保留最新内容；关闭或分段结束时按配置压缩。写端只依赖标准库，
不认识 LLS 的会话、模型或界面。
"""

from __future__ import annotations

import base64
import contextlib
import gzip
import json
import os
import secrets
import shutil
import threading
import time
from pathlib import Path
from typing import TextIO

from .format import CHANNEL_RESIZE, CHANNELS, FORMAT_VERSION, parse_resize
from .model import TerminalRef

__all__ = [
    "DEFAULT_MAX_RECORDING_BYTES",
    "DEFAULT_MAX_TOTAL_BYTES",
    "TerminalRecorder",
]

DEFAULT_MAX_RECORDING_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_TOTAL_BYTES = 1024 * 1024 * 1024


def _private_append(path: Path) -> TextIO:
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)
    return os.fdopen(fd, "a", encoding="utf-8", newline="\n")


def _private_write(path: Path, text: str) -> None:
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def _private_replace(path: Path, text: str) -> None:
    temp = path.with_name(path.name + ".tmp")
    _private_write(temp, text)
    os.replace(temp, path)


class TerminalRecorder:
    """把一次终端启动写成同一 recording 下的多个分段。"""

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        recording_id: str | None = None,
        rows: int | None = None,
        columns: int | None = None,
        compress: bool = True,
        max_recording_bytes: int = DEFAULT_MAX_RECORDING_BYTES,
        max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
        fsync: bool = True,
    ) -> None:
        self.root = Path(root)
        if recording_id is None:
            recording_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            recording_id += "-" + secrets.token_hex(4)
        if (
            not recording_id
            or recording_id in (".", "..")
            or "/" in recording_id
            or "\\" in recording_id
        ):
            raise ValueError(f"bad recording id: {recording_id!r}")
        self.recording_id = recording_id
        self._compress = compress
        self._max_recording_bytes = max(1024, int(max_recording_bytes))
        self._max_total_bytes = max(0, int(max_total_bytes))
        self._fsync = fsync
        self._lock = threading.RLock()
        self._closed = False
        self._closed_at: float | None = None
        self._frame = 0
        self._segment_no = 0
        self._segments: list[str] = []
        self._created_at = time.time()
        self._t0 = time.monotonic()
        self._time_base = 0.0
        self._started = self._t0
        self._last_at = 0.0
        self._initial_rows = rows if rows is not None and rows > 0 else None
        self._initial_columns = columns if columns is not None and columns > 0 else None
        self._rows = self._initial_rows
        self._columns = self._initial_columns
        self.dir = self.root / self.recording_id
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        with contextlib.suppress(OSError):
            os.chmod(self.dir, 0o700)
        self._recover()
        self._index = _private_append(self.dir / "index.jsonl")
        self._open_segment()

    # ---- 恢复 ----

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
            except (OSError, EOFError, StopIteration):
                packed.unlink(missing_ok=True)
                continue
            source.unlink(missing_ok=True)

    # ---- 分段 ----

    def _segment_name(self, no: int) -> str:
        return f"segment-{no:06d}.cast"

    def _segment_path(self, name: str) -> Path:
        return self.dir / name

    def _open_segment(self) -> None:
        self._segment_no += 1
        name = self._segment_name(self._segment_no)
        self._stream_name = name
        self._stream = _private_append(self._segment_path(name))
        self._started = time.monotonic()
        self._time_base = round(self._started - self._t0, 6)
        self._segments.append(name)
        self._append_line(
            self._stream,
            {
                "version": FORMAT_VERSION,
                "id": self.recording_id,
                "segment": name,
                "created_at": self._created_at,
                "time_base": round(self._time_base, 6),
                "first_frame": self._frame + 1,
                "rows": self._rows,
                "columns": self._columns,
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
        if not path.is_file():
            return
        if self._compress:
            final = path.with_suffix(".cast.gz")
            self._compress_segment(path, final)
            path.unlink(missing_ok=True)
            self._rewrite_index(self._stream_name, final.name)
            self._segments = [
                final.name if item == self._stream_name else item for item in self._segments
            ]
        self._write_meta()

    def _compress_segment(self, source: Path, target: Path) -> None:
        temp = target.with_name(target.name + ".tmp")
        fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with contextlib.ExitStack() as stack:
            raw = stack.enter_context(os.fdopen(fd, "wb"))
            writer = stack.enter_context(
                gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=6)
            )
            reader = stack.enter_context(source.open("rb"))
            shutil.copyfileobj(reader, writer)
        os.replace(temp, target)

    def _rewrite_index(self, old: str, new: str) -> None:
        self._index.flush()
        self._index.close()
        index = self.dir / "index.jsonl"
        kept: list[str] = []
        for line in index.read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if item.get("segment") == old:
                item["segment"] = new
            kept.append(json.dumps(item, ensure_ascii=False))
        _private_replace(index, "\n".join(kept) + ("\n" if kept else ""))
        self._index = _private_append(index)

    def _drop_segment(self, name: str) -> None:
        path = self._segment_path(name)
        self._segments.remove(name)
        self._index.flush()
        self._index.close()
        kept: list[str] = []
        for line in (self.dir / "index.jsonl").read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if item.get("segment") != name:
                kept.append(line)
        index = self.dir / "index.jsonl"
        _private_replace(index, "\n".join(kept) + ("\n" if kept else ""))
        self._index = _private_append(index)
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
        payload = {
            "version": FORMAT_VERSION,
            "id": self.recording_id,
            "created_at": self._created_at,
            "closed_at": self._closed_at,
            "rows": self._initial_rows,
            "columns": self._initial_columns,
            "compressed": self._compress,
            "frames": self._frame,
            "duration": round(self._last_at, 6),
            "retained_from": self._retained_from(),
            "segments": list(self._segments),
            "bytes": self._segment_bytes(),
        }
        target = self.dir / "meta.json"
        _private_replace(target, json.dumps(payload, ensure_ascii=False) + "\n")

    def _retained_from(self) -> int:
        first = None
        for line in (self.dir / "index.jsonl").read_text(encoding="utf-8").splitlines():
            try:
                frame = int(json.loads(line)["frame"])
            except (ValueError, KeyError, TypeError):
                continue
            first = frame if first is None else min(first, frame)
        return first or 1

    # ---- 写入 ----

    @property
    def current_ref(self) -> TerminalRef:
        with self._lock:
            return TerminalRef(self.recording_id, self._frame)

    def record(self, channel: str, data: bytes | str) -> TerminalRef:
        """写入一帧；大块字节按单次上限切帧。"""
        if channel not in CHANNELS:
            raise ValueError("channel must be one of {}".format(", ".join(CHANNELS)))
        payload = data.encode("utf-8") if isinstance(data, str) else bytes(data)
        with self._lock:
            if self._closed:
                raise RuntimeError("recorder is closed")
            if not payload:
                return self.current_ref
            if channel == CHANNEL_RESIZE:
                size = parse_resize(payload)
                if size is not None:
                    self._rows, self._columns = size
            chunk_size = max(1, self._max_recording_bytes // 2)
            ref = self.current_ref
            for start in range(0, len(payload), chunk_size):
                chunk = payload[start : start + chunk_size]
                used = self._stream.tell()
                if used and used + len(chunk) > self._max_recording_bytes:
                    self._rotate()
                ref = self._write_frame(channel, chunk)
            return ref

    def _write_frame(self, channel: str, payload: bytes) -> TerminalRef:
        offset = self._stream.tell()
        relative = round(time.monotonic() - self._started, 6)
        at = self._time_base + relative
        self._frame += 1
        self._last_at = max(self._last_at, at)
        event = [relative, channel, base64.b64encode(payload).decode("ascii")]
        self._append_line(self._stream, event)
        self._append_line(
            self._index,
            {
                "frame": self._frame,
                "segment": self._stream_name,
                "time": event[0],
                "at": round(at, 6),
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
            self._closed_at = time.time()
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
            except (OSError, ValueError, KeyError, TypeError):
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
        with contextlib.closing(_private_append(path)) as handle:
            handle.write(
                json.dumps(
                    {"recording_id": recording_id, "deleted_at": time.time()},
                    ensure_ascii=False,
                )
                + "\n"
            )
            handle.flush()
            os.fsync(handle.fileno())

    # ---- 行工具 ----

    @staticmethod
    def _append_line(handle: TextIO, value: object) -> None:
        handle.write(json.dumps(value, ensure_ascii=False) + "\n")
