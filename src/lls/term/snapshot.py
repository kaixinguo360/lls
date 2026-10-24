"""屏幕快照：golden 测试的序列化与差异比对。

快照是「某一时刻屏幕状态」的无损 JSON 表示，用于：

* 把旧实现（legacy ``terminal.Screen``）与 pyte 适配器在同一语料下对拍；
* 把录制好的 golden 基线固化到 ``tests/golden/*.json``；
* ``lls replay`` 回放时打印人类可读的差异。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Sequence

from .model import ScreenModel

__all__ = ["Snapshot", "capture", "diff", "load", "dump", "dumps"]


@dataclass(frozen=True)
class Snapshot:
    """一次屏幕快照。``id`` 用于标识采样点（如语料内的字节偏移）。"""

    id: str
    rows: int
    columns: int
    cursor: tuple[int, int, bool]  # (x, y, hidden)
    alternate: bool
    lines: tuple[str, ...]
    scrollback: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Snapshot":
        return cls(
            id=d["id"],
            rows=d["rows"],
            columns=d["columns"],
            cursor=tuple(d["cursor"]),
            alternate=d["alternate"],
            lines=tuple(d["lines"]),
            scrollback=tuple(d["scrollback"]),
        )


def capture(model: ScreenModel, id: str) -> Snapshot:
    """从任意 ScreenModel 实现采样快照。"""
    return Snapshot(
        id=id,
        rows=model.rows,
        columns=model.columns,
        cursor=(model.cursor.x, model.cursor.y, model.cursor.hidden),
        alternate=model.alternate,
        lines=tuple(model.lines()),
        scrollback=tuple(model.scrollback()),
    )


def dumps(s: Snapshot) -> str:
    return json.dumps(s.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)


def dump(s: Snapshot, path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(dumps(s))


def load(path: str) -> Snapshot:
    with open(path, "r", encoding="utf-8") as f:
        return Snapshot.from_dict(json.load(f))


def diff(a: Snapshot, b: Snapshot) -> list[str]:
    """返回两快照的结构化差异；空列表表示一致。"""
    out: list[str] = []
    for name, va, vb in (
        ("rows", a.rows, b.rows),
        ("columns", a.columns, b.columns),
        ("cursor", a.cursor, b.cursor),
        ("alternate", a.alternate, b.alternate),
    ):
        if va != vb:
            out.append(f"{name}: {va!r} != {vb!r}")
    _diff_sequence(out, "lines", a.lines, b.lines)
    _diff_sequence(out, "scrollback", a.scrollback, b.scrollback)
    return out


def _diff_sequence(
    out: list[str], name: str, a: Sequence[str], b: Sequence[str]
) -> None:
    if len(a) != len(b):
        out.append(f"{name}: len {len(a)} != {len(b)}")
        return
    for i, (la, lb) in enumerate(zip(a, b)):
        if la != lb:
            out.append(f"{name}[{i}]: {la!r} != {lb!r}")
