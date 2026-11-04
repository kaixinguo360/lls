"""文件工具：不经过终端，直接从磁盘读、改、写。

补上屏幕之外的通道 —— 日志、源码这类东西不必先 ``cat`` 到终端再回读，也不会
因为滚出屏幕而看不到。三个工具都不碰终端、不碰子进程，唯一的外部状态是文件
系统，因此也不依赖任何端口（见 :py:mod:`lls.ai.tools.terminal` 里那组工具）。

写文件先写临时文件再整体替换：写到一半被杀（Ctrl-C、断电）也不会留下半截
文件，另一半还是原来的内容。``edit_file`` 走的是同一条路。
"""

from __future__ import annotations

import os
from typing import Any

from ..agent import AgentTool
from ..types import Tool
from .args import as_int

__all__ = [
    "EDIT_FILE_TOOL",
    "READ_FILE_TOOL",
    "WRITE_FILE_TOOL",
    "file_tools",
]

MAX_FILE_CHARS = 200_000  # read_file 一次最多回多少字符：再多就该分段读
MAX_FILE_LINES = 2000     # read_file 的 max_lines 上限
DEFAULT_FILE_LINES = 200  # read_file 未给 max_lines 时的取值


READ_FILE_TOOL = Tool(
    name="read_file",
    description=(
        "Read a text file straight from disk (UTF-8, undecodable bytes shown as "
        "replacement characters). Each line is prefixed with its 1-based line "
        "number. start_line/max_lines take a slice; the footer says which "
        "start_line continues after a truncated read. Cheaper and more complete "
        "than cat-ing the file into the terminal."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File to read; ~ is expanded.",
            },
            "start_line": {
                "type": "integer",
                "minimum": 1,
                "description": "First line to return, 1-based.",
            },
            "max_lines": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_FILE_LINES,
                "description": "How many lines to return at most; 200 is a good default.",
            },
        },
        "required": ["path", "start_line", "max_lines"],
        "additionalProperties": False,
    },
    strict=True,
)

WRITE_FILE_TOOL = Tool(
    name="write_file",
    description=(
        "Write a text file straight to disk (UTF-8); missing parent directories "
        "are created. append=false replaces the whole file atomically. Use it for "
        "scripts, configs and notes instead of typing them into an editor."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File to write; ~ is expanded.",
            },
            "content": {
                "type": "string",
                "description": "Full text to write (append=true: text to add).",
            },
            "append": {
                "type": "boolean",
                "description": "true adds to the end instead of replacing the file.",
            },
        },
        "required": ["path", "content", "append"],
        "additionalProperties": False,
    },
    strict=True,
)

EDIT_FILE_TOOL = Tool(
    name="edit_file",
    description=(
        "Replace literal text in an existing UTF-8 file and write it back "
        "atomically. old_string must appear exactly once unless replace_all is "
        "true; zero or several matches fail with an explanation instead of "
        "guessing. Prefer it over rewriting the whole file with write_file."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File to edit; ~ is expanded.",
            },
            "old_string": {
                "type": "string",
                "description": "Exact literal text to replace (not a regex).",
            },
            "new_string": {
                "type": "string",
                "description": "Replacement text; empty string deletes the match.",
            },
            "replace_all": {
                "type": "boolean",
                "description": "Replace every occurrence instead of requiring exactly one.",
            },
        },
        "required": ["path", "old_string", "new_string", "replace_all"],
        "additionalProperties": False,
    },
    strict=True,
)


def _path_argument(args: dict[str, Any]) -> str:
    """取 ``path`` 参数：去空白、展开 ``~``。"""
    raw = args.get("path")
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("'path' must be a non-empty string")
    return os.path.expanduser(raw)


def _write_atomic(path: str, data: bytes) -> None:
    """先写临时文件再整体替换；失败时删掉临时文件，原文件不动。"""
    temp = f"{path}.lls-tmp-{os.getpid()}"
    try:
        with open(temp, "wb") as handle:
            handle.write(data)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


async def read_file(args: dict[str, Any]) -> str:
    """按行号取一段：正文带行号，页脚说清下一次从哪接着读。

    逐行读、逐行跳过，只把要返回的那一段留在内存里 —— 对十万行的日志从
    第 90000 行开始读，和从第 1 行读一样快，也不会因为「只读了文件头」而
    谎报后面没有内容。
    """
    path = _path_argument(args)
    start = max(1, as_int(args.get("start_line"), 1))
    limit = max(
        1, min(as_int(args.get("max_lines"), DEFAULT_FILE_LINES), MAX_FILE_LINES)
    )
    lines: list[tuple[int, str]] = []
    total: int | None = None  # None = 还没读到末尾
    used = 0
    number = 0
    try:
        with open(path, "rb") as handle:
            for number, raw in enumerate(handle, start=1):
                if b"\x00" in raw:
                    raise ValueError(f"{path} looks binary (contains NUL bytes)")
                if number < start:
                    continue
                text = raw.decode("utf-8", "replace").rstrip("\r\n")
                if lines and (
                    len(lines) >= limit or used + len(text) + 6 > MAX_FILE_CHARS
                ):
                    break
                lines.append((number, text))
                used += len(text) + 6
            else:
                total = number  # 循环自然走完：最后一行号就是总行数
    except OSError as e:
        raise ValueError(f"cannot read {path}: {e.strerror or e}") from e

    if not lines:
        return f"{path}（共 {total if total is not None else 0} 行；第 {start} 行之后没有内容）"
    first, last = lines[0][0], lines[-1][0]
    body = "\n".join(f"{number}: {text}" for number, text in lines)
    if total is not None:
        return f"{path}（第 {first}-{last} 行，共 {total} 行）\n{body}"
    return (
        f"{path}（第 {first}-{last} 行，后面还有）\n"
        f"{body}\n\n（用 start_line={last + 1} 接着读）"
    )


async def write_file(args: dict[str, Any]) -> str:
    """整写或追加；覆盖时先写临时文件再 ``os.replace``。"""
    path = _path_argument(args)
    content = args.get("content")
    if not isinstance(content, str):
        raise ValueError("'content' must be a string")
    append = args.get("append", False)
    if not isinstance(append, bool):
        raise ValueError("'append' must be a boolean")
    data = content.encode("utf-8")
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        if append:
            with open(path, "ab") as handle:
                handle.write(data)
        else:
            _write_atomic(path, data)
    except OSError as e:
        raise ValueError(f"cannot write {path}: {e.strerror or e}") from e
    rows = len(content.splitlines())
    return "已{} {} 行 / {} 字节：{}".format(
        "追加到" if append else "写入", rows, len(data), path
    )


async def edit_file(args: dict[str, Any]) -> str:
    """按字面替换；默认要求恰好一处，多处时让模型给更长的上下文。"""
    path = _path_argument(args)
    old = args.get("old_string")
    new = args.get("new_string")
    replace_all = args.get("replace_all", False)
    if not isinstance(old, str):
        raise ValueError("'old_string' must be a string")
    if not isinstance(new, str):
        raise ValueError("'new_string' must be a string")
    if not isinstance(replace_all, bool):
        raise ValueError("'replace_all' must be a boolean")
    if not old:
        raise ValueError("'old_string' must be a non-empty string")
    if old == new:
        raise ValueError("'old_string' and 'new_string' are identical")

    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError as e:
        raise ValueError(f"cannot read {path}: {e.strerror or e}") from e
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ValueError(f"{path} is not valid UTF-8: {e}") from e

    count = text.count(old)
    if count == 0:
        raise ValueError(
            f"'{old}' does not appear in {path}; read the file and copy the exact text"
        )
    if count > 1 and not replace_all:
        raise ValueError(
            f"'{old}' appears {count} times in {path}; "
            "add surrounding context or set replace_all=true"
        )
    updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)
    try:
        _write_atomic(path, updated.encode("utf-8"))
    except OSError as e:
        raise ValueError(f"cannot write {path}: {e.strerror or e}") from e
    return "已替换 {} 处：{}（{} 行）".format(
        count if replace_all else 1, path, len(updated.splitlines())
    )


def file_tools() -> list[AgentTool]:
    """文件这一族：只依赖文件系统，谁都能用。"""
    return [
        AgentTool(READ_FILE_TOOL, read_file),
        AgentTool(EDIT_FILE_TOOL, edit_file),
        AgentTool(WRITE_FILE_TOOL, write_file),
    ]
