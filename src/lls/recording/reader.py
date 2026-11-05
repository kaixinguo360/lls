"""录制读取端：把磁盘上的分段还原成带绝对时间的帧序列。

读取端只依赖标准库：不解码终端控制序列，也不认识 pyte。损坏的尾行与中途
断掉的 gzip 只影响当前分段，已读出的帧照常返回。
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import gzip
import json
import os
from pathlib import Path
from typing import Collection, Iterator

from .errors import FrameUnavailable, RecordingCorrupt, RecordingNotFound
from .format import (
    CHANNEL_RESIZE,
    FORMAT_VERSION,
    SEGMENT_RE,
    absolute_time,
    parse_resize,
)
from .model import Frame, RecordingMeta

__all__ = ["Recording", "tombstone_time"]


def _number(value: object, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)


def _integer(value: object, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value


class Recording:
    """一个录制目录的只读视图。"""

    def __init__(
        self,
        directory: Path,
        meta: RecordingMeta,
        segments: tuple[Path, ...],
        warnings: list[str],
    ) -> None:
        self.directory = directory
        self.meta = meta
        self._segments = segments
        self._warnings = list(warnings)

    # ---- 打开 ----

    @classmethod
    def open(cls, directory: str | os.PathLike[str]) -> Recording:
        path = Path(directory).expanduser()
        if not path.is_dir():
            raise RecordingNotFound(path)
        meta = _read_meta(path)
        segments, warnings = _discover(path, meta)
        return cls(path, meta, segments, warnings)

    @property
    def warnings(self) -> tuple[str, ...]:
        return tuple(self._warnings)

    @property
    def geometry(self) -> tuple[int, int] | None:
        return self.meta.geometry

    # ---- 帧 ----

    def frames(
        self,
        *,
        start: int = 1,
        stop: int | None = None,
        channels: Collection[str] | None = None,
    ) -> Iterator[Frame]:
        """按段顺序产出帧；``start``/``stop`` 是全局帧号（含端点）。"""
        selected = frozenset(channels) if channels is not None else None
        for path in self._segments:
            number: int | None = None
            base = 0.0
            for header, item in self._items(path):
                if number is None:
                    number = _integer(header.get("first_frame"), 1)
                    base = _number(header.get("time_base"), 0.0)
                relative, channel, payload = item
                if isinstance(relative, bool) or not isinstance(relative, (int, float)):
                    self._warn(f"跳过时间字段损坏的帧：{path.name}")
                    continue
                if not isinstance(channel, str) or not isinstance(payload, str):
                    self._warn(f"跳过形状损坏的帧：{path.name}")
                    continue
                try:
                    data = base64.b64decode(payload, validate=True)
                except (ValueError, binascii.Error):
                    self._warn(f"跳过 base64 损坏的帧：{path.name}")
                    continue
                at = round(absolute_time(base, float(relative)), 6)
                if number >= start:
                    if stop is not None and number > stop:
                        return
                    if selected is None or channel in selected:
                        yield Frame(number, at, channel, data)
                number += 1

    def frame(self, number: int) -> Frame:
        earliest = max(1, self.meta.retained_from)
        if number < earliest:
            raise FrameUnavailable(number, earliest)
        for frame in self.frames(start=number, stop=number):
            return frame
        raise FrameUnavailable(number, earliest)

    def frame_time(self, number: int) -> float:
        entry = self._index_entry(number)
        if entry is not None:
            at = entry.get("at")
            if not isinstance(at, bool) and isinstance(at, (int, float)):
                return float(at)
        return self.frame(number).at

    def duration(self) -> float:
        if self.meta.duration > 0:
            return self.meta.duration
        last = 0.0
        for frame in self.frames():
            last = frame.at
        return last

    def geometry_at(self, at: float) -> tuple[int, int] | None:
        size = self.geometry
        for frame in self.frames(channels={CHANNEL_RESIZE}):
            if frame.at > at:
                break
            parsed = parse_resize(frame.data)
            if parsed is not None:
                size = parsed
        return size

    # ---- 分段 ----

    def _items(self, path: Path) -> Iterator[tuple[dict, list]]:
        try:
            with contextlib.ExitStack() as stack:
                if path.suffix == ".gz":
                    handle = stack.enter_context(gzip.open(path, "rt", encoding="utf-8"))
                else:
                    handle = stack.enter_context(path.open("r", encoding="utf-8"))
                header: dict | None = None
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        item = json.loads(line)
                    except ValueError:
                        self._warn(f"跳过损坏的行：{path.name}")
                        continue
                    if isinstance(item, dict):
                        if header is None:
                            header = self._header(item, path)
                            if header is None:
                                return
                        continue
                    if header is None:
                        self._warn(f"分段缺少段头：{path.name}")
                        return
                    if not isinstance(item, list) or len(item) != 3:
                        self._warn(f"跳过形状损坏的帧：{path.name}")
                        continue
                    yield header, item
        except (OSError, EOFError, UnicodeError) as error:
            self._warn(f"分段读取中断：{path.name}（{error}）")

    def _header(self, item: dict, path: Path) -> dict | None:
        if item.get("version") != FORMAT_VERSION:
            self._warn(f"跳过版本不匹配的分段：{path.name}（{item.get('version')}）")
            return None
        base = item.get("time_base")
        first = item.get("first_frame")
        if isinstance(base, bool) or not isinstance(base, (int, float)):
            self._warn(f"分段缺少 time_base：{path.name}")
            return None
        if isinstance(first, bool) or not isinstance(first, int) or first < 1:
            self._warn(f"分段缺少 first_frame：{path.name}")
            return None
        return item

    # ---- 索引 ----

    def _index_entry(self, number: int) -> dict | None:
        path = self.directory / "index.jsonl"
        try:
            handle = path.open("r", encoding="utf-8")
        except OSError:
            return None
        with handle:
            for line in handle:
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if isinstance(item, dict) and item.get("frame") == number:
                    return item
        return None

    def _warn(self, message: str) -> None:
        if message not in self._warnings:
            self._warnings.append(message)


def _read_meta(directory: Path) -> RecordingMeta:
    path = directory / "meta.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise RecordingCorrupt(directory, f"meta.json 无法读取：{error}") from error
    except ValueError as error:
        raise RecordingCorrupt(directory, f"meta.json 不是合法 JSON：{error}") from error
    if not isinstance(raw, dict):
        raise RecordingCorrupt(directory, "meta.json 不是对象")
    version = raw.get("version")
    if version != FORMAT_VERSION:
        raise RecordingCorrupt(
            directory, f"不支持的录制版本：{version!r}"
        )
    recording_id = raw.get("id")
    if not isinstance(recording_id, str) or not recording_id:
        raise RecordingCorrupt(directory, "meta.json 缺少 id")
    segments = raw.get("segments")
    names: tuple[str, ...] = ()
    if isinstance(segments, list):
        names = tuple(name for name in segments if isinstance(name, str))
    return RecordingMeta(
        id=recording_id,
        created_at=_number(raw.get("created_at"), 0.0),
        closed_at=(
            None
            if raw.get("closed_at") is None
            else _number(raw.get("closed_at"), 0.0)
        ),
        rows=_optional_positive(raw.get("rows")),
        columns=_optional_positive(raw.get("columns")),
        compressed=bool(raw.get("compressed", False)),
        frames=_integer(raw.get("frames"), 0),
        duration=_number(raw.get("duration"), 0.0),
        retained_from=max(1, _integer(raw.get("retained_from"), 1)),
        segments=names,
        bytes=_integer(raw.get("bytes"), 0),
    )


def _optional_positive(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _discover(
    directory: Path, meta: RecordingMeta
) -> tuple[tuple[Path, ...], list[str]]:
    warnings: list[str] = []
    found: dict[int, Path] = {}
    try:
        entries = list(directory.iterdir())
    except OSError as error:
        raise RecordingCorrupt(directory, f"目录无法读取：{error}") from error
    for item in entries:
        match = SEGMENT_RE.match(item.name)
        if match is None or not item.is_file():
            continue
        number = int(match.group(1))
        if item.name.endswith(".gz") or number not in found:
            found[number] = item
    segments = tuple(found[number] for number in sorted(found))
    if not segments:
        raise RecordingCorrupt(directory, "没有可读分段")
    numbers = sorted(found)
    for previous, current in zip(numbers, numbers[1:]):
        if current != previous + 1:
            warnings.append(f"分段编号不连续：{previous} → {current}")
    present = {item.name for item in segments}
    for name in meta.segments:
        if name not in present:
            warnings.append(f"缺少分段：{name}")
    return segments, warnings


def tombstone_time(root: str | os.PathLike[str], recording_id: str) -> float | None:
    """查总量上限留下的删除记录；没有记录返回 ``None``。"""
    path = Path(root).expanduser() / "tombstones.jsonl"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if not isinstance(item, dict) or item.get("recording_id") != recording_id:
            continue
        deleted = item.get("deleted_at")
        return _number(deleted, 0.0)
    return None
