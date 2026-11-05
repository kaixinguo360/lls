"""``lls recording`` 的命令行：``ls`` / ``play`` / ``export``。

这个模块属于录制包，不 import ``lls.*``；LLS 入口只把配置里的录制目录和
屏幕/渲染适配器传进来。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable, Sequence

from .errors import RecordingDeleted, RecordingError, RecordingNotFound
from .reader import Recording, tombstone_time

__all__ = ["main"]

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def default_root() -> Path:
    return Path.home() / ".local" / "lls" / "recordings"


def _positive_float(text: str) -> float:
    try:
        value = float(text)
    except ValueError as error:
        raise argparse.ArgumentTypeError("需要一个数字") from error
    if value <= 0:
        raise argparse.ArgumentTypeError("必须大于 0")
    return value


def _size(text: str) -> tuple[int, int]:
    columns, separator, rows = text.lower().partition("x")
    if not separator:
        raise argparse.ArgumentTypeError("尺寸格式应为 COLSxROWS，如 160x48")
    try:
        width = int(columns)
        height = int(rows)
    except ValueError as error:
        raise argparse.ArgumentTypeError("尺寸格式应为 COLSxROWS，如 160x48") from error
    if width <= 0 or height <= 0:
        raise argparse.ArgumentTypeError("尺寸必须为正")
    return height, width


def _build_parser(root: str | os.PathLike[str] | None) -> argparse.ArgumentParser:
    default = str(Path(root).expanduser() if root is not None else default_root())
    parser = argparse.ArgumentParser(
        prog="lls recording",
        description="终端录制的列出、回放与导出",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    listing = sub.add_parser("ls", aliases=["list"], help="列出录制")
    listing.add_argument("--root", default=default, help="录制根目录")
    listing.add_argument("--json", action="store_true", help="输出 JSON")

    play = sub.add_parser("play", help="回放一条录制")
    play.add_argument("id", metavar="ID|PATH", help="录制 id 或目录")
    play.add_argument("--root", default=default, help="录制根目录")
    play.add_argument("--speed", type=_positive_float, default=1.0, help="播放倍速")
    target = play.add_mutually_exclusive_group()
    target.add_argument("--frame", type=int, help="从指定帧开始")
    target.add_argument("--time", type=float, help="从指定秒数开始")
    play.add_argument("--size", type=_size, help="录制尺寸，COLSxROWS")
    play.add_argument("--show-input", action="store_true", help="在状态栏显示输入")
    play.add_argument("--dump", action="store_true", help="只输出目标位置的文本")

    export = sub.add_parser("export", help="导出为 asciicast v2")
    export.add_argument("id", metavar="ID|PATH", help="录制 id 或目录")
    export.add_argument("--root", default=default, help="录制根目录")
    export.add_argument("-o", "--output", default="-", help="输出文件，默认 stdout")
    export.add_argument("--with-input", action="store_true", help="一并导出输入事件")
    export.add_argument("--size", type=_size, help="录制尺寸，COLSxROWS")
    return parser


def _resolve(root: Path, value: str) -> Path:
    """id 在 ``root`` 下找；带分隔符的值按路径处理。"""
    explicit = (
        os.path.isabs(value)
        or "/" in value
        or "\\" in value
        or value.startswith(("~", "."))
    )
    if explicit:
        path = Path(value).expanduser()
        if path.is_dir():
            return path
        raise RecordingNotFound(path)
    if not _ID_RE.match(value):
        raise RecordingNotFound(value)
    path = root / value
    if path.is_dir():
        _ensure_inside(root, path)
        return path
    deleted = tombstone_time(root, value)
    if deleted is not None:
        raise RecordingDeleted(value, deleted)
    raise RecordingNotFound(path)


def _ensure_inside(root: Path, path: Path) -> None:
    base = root.expanduser().resolve()
    target = path.resolve()
    if target != base and base not in target.parents:
        raise RecordingNotFound(path)


def _list(root: Path, as_json: bool) -> int:
    entries: list[dict] = []
    if root.is_dir():
        for path in sorted(root.iterdir()):
            if not path.is_dir() or path.name.startswith("."):
                continue
            if not (path / "meta.json").is_file():
                continue
            try:
                recording = Recording.open(path)
            except RecordingError as error:
                entries.append(
                    {"id": path.name, "state": "损坏", "error": str(error)}
                )
                continue
            meta = recording.meta
            if meta.closed_at is None:
                state = "未正常结束"
            elif meta.retained_from > 1:
                state = f"已滚出 {meta.retained_from - 1} 帧"
            else:
                state = "正常"
            entries.append(
                {
                    "id": meta.id,
                    "created_at": meta.created_at,
                    "closed_at": meta.closed_at,
                    "duration": meta.duration,
                    "rows": meta.rows,
                    "columns": meta.columns,
                    "frames": meta.frames,
                    "bytes": meta.bytes,
                    "retained_from": meta.retained_from,
                    "state": state,
                }
            )
    entries.sort(key=lambda item: float(item.get("created_at") or 0.0), reverse=True)

    if as_json:
        print(json.dumps(entries, ensure_ascii=False, indent=2))
        return 0
    if not entries:
        print("没有录制")
        return 0
    print(
        "{:<26} {:<19} {:>8} {:>9} {:>9} {:>6}  {}".format(
            "ID", "开始时间", "时长", "大小", "尺寸", "帧数", "状态"
        )
    )
    for item in entries:
        if "error" in item:
            print(
                "{:<26} {:<19} {:>8} {:>9} {:>9} {:>6}  {}".format(
                    item["id"], "-", "-", "-", "-", "-", item["state"]
                )
            )
            continue
        geometry = "-"
        if item["columns"] and item["rows"]:
            geometry = "{}x{}".format(item["columns"], item["rows"])
        print(
            "{:<26} {:<19} {:>8} {:>9} {:>9} {:>6}  {}".format(
                item["id"],
                _format_time(item["created_at"]),
                _format_duration(item["duration"]),
                _human_size(item["bytes"]),
                geometry,
                item["frames"],
                item["state"],
            )
        )
    return 0


def _report_warnings(recording: Recording) -> None:
    for warning in recording.warnings:
        print(f"lls recording: {warning}", file=sys.stderr)


def _play(
    args: argparse.Namespace,
    screen_factory: Callable | None,
    row_renderer: Callable | None,
) -> int:
    root = Path(args.root).expanduser()
    path = _resolve(root, args.id)
    recording = Recording.open(path)
    _report_warnings(recording)
    size = args.size or recording.geometry
    if size is None:
        raise RecordingError("录制没有尺寸，请用 --size 指定")
    if args.frame is not None:
        recording.frame_time(args.frame)  # 验证帧存在且在保留窗口内
    start_at = args.time if args.time is not None else 0.0

    if args.dump:
        from .replay.player import Playback

        playback = Playback(recording, size=size, screen_factory=screen_factory)
        if args.frame is not None:
            playback.seek_frame(args.frame)
        elif args.time is not None:
            playback.seek(args.time)
        else:
            playback.seek(float("inf"))
        text = playback.screen.text()
        if text:
            print(text)
        return 0

    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise RecordingError("play 需要交互终端；请改用 --dump 或 export")
    from .replay.tui import run

    return run(
        recording,
        size=size,
        start_at=start_at,
        start_frame=args.frame,
        speed=args.speed,
        show_input=args.show_input,
        screen_factory=screen_factory,
        row_renderer=row_renderer,
    )


def _export(args: argparse.Namespace) -> int:
    root = Path(args.root).expanduser()
    recording = Recording.open(_resolve(root, args.id))
    _report_warnings(recording)
    size = args.size or recording.geometry
    from .replay.export import export_asciicast

    if args.output == "-":
        export_asciicast(recording, sys.stdout, with_input=args.with_input, size=size)
        return 0
    with open(args.output, "w", encoding="utf-8", newline="\n") as handle:
        export_asciicast(recording, handle, with_input=args.with_input, size=size)
    return 0


def main(
    argv: Sequence[str] | None = None,
    *,
    root: str | os.PathLike[str] | None = None,
    screen_factory: Callable | None = None,
    row_renderer: Callable | None = None,
) -> int:
    parser = _build_parser(root)
    args = parser.parse_args(argv)
    try:
        if args.cmd in ("ls", "list"):
            return _list(Path(args.root).expanduser(), args.json)
        if args.cmd == "play":
            return _play(args, screen_factory, row_renderer)
        return _export(args)
    except RecordingError as error:
        print(f"lls recording: {error}", file=sys.stderr)
        return 1
    except OSError as error:
        print(f"lls recording: {error}", file=sys.stderr)
        return 1


def _format_time(value: float) -> str:
    if not value:
        return "-"
    return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")


def _format_duration(seconds: float) -> str:
    total = int(max(0.0, seconds))
    if total >= 3600:
        return f"{total // 3600}:{total % 3600 // 60:02d}:{total % 60:02d}"
    return f"{total // 60:02d}:{total % 60:02d}"


def _human_size(value: int) -> str:
    size = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            if unit == "B":
                return f"{size:.0f} B"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"
