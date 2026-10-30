"""文件工具：不经过终端，直接从磁盘读、往磁盘写。

补上屏幕之外的通道 —— 日志、源码这类东西不必先 ``cat`` 到终端再回读，也不会
因为滚出屏幕而看不到。两个工具都不碰终端、不碰子进程，唯一的外部状态是文件
系统，因此也不依赖任何端口（见 :py:mod:`lls.ai.tools.terminal` 里那组工具）。

写文件先写临时文件再整体替换：写到一半被杀（Ctrl-C、断电）也不会留下半截
文件，另一半还是原来的内容。
"""

from __future__ import annotations

import os
from typing import Any

from ..agent import AgentTool
from ..types import Tool
from .args import as_int

__all__ = ["READ_FILE_TOOL", "WRITE_FILE_TOOL", "file_tools"]

MAX_FILE_CHARS = 200_000  # read_file 一次最多回多少字符：再多就该分段读
MAX_FILE_LINES = 2000     # read_file 的 max_lines 上限
DEFAULT_FILE_LINES = 200  # read_file 未给 max_lines 时的取值


READ_FILE_TOOL = Tool(
    name="read_file",
    description=(
        "Read a text file straight from disk (UTF-8, undecodable bytes shown as "
        "replacement characters). Lines are 1-based; start_line/max_lines take a "
        "slice, and very long slices are truncated. Cheaper and more complete "
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


def _path_argument(args: dict[str, Any]) -> str:
    """取 ``path`` 参数：去空白、展开 ``~``。"""
    raw = args.get("path")
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("'path' must be a non-empty string")
    return os.path.expanduser(raw)


async def read_file(args: dict[str, Any]) -> str:
    """按行号取一段；头部一句说明这是文件哪一段、共多少行。"""
    path = _path_argument(args)
    start = max(1, as_int(args.get("start_line"), 1))
    limit = max(1, min(as_int(args.get("max_lines"), DEFAULT_FILE_LINES), MAX_FILE_LINES))
    try:
        with open(path, "rb") as handle:
            data = handle.read(MAX_FILE_CHARS * 4)
    except OSError as e:
        raise ValueError(f"cannot read {path}: {e.strerror or e}") from e
    text = data.decode("utf-8", "replace")
    if "\x00" in text:
        raise ValueError(f"{path} looks binary (contains NUL bytes)")
    rows = text.splitlines()
    chunk = rows[start - 1 : start - 1 + limit]
    if not chunk:
        head = f"{path}（共 {len(rows)} 行；第 {start} 行之后没有内容）"
    else:
        head = f"{path}（第 {start}-{start + len(chunk) - 1} 行，共 {len(rows)} 行）"
    body = "\n".join(chunk)
    note = ""
    if len(body) > MAX_FILE_CHARS:
        body = body[:MAX_FILE_CHARS]
        note = "\n\n（内容过长，已截断；用 start_line 接着读）"
    return head + "\n" + body + note


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
            # 先写临时文件再整体替换：写到一半被杀也不会留下半截文件。
            temp = f"{path}.lls-tmp-{os.getpid()}"
            try:
                with open(temp, "wb") as handle:
                    handle.write(data)
                os.replace(temp, path)
            finally:
                if os.path.exists(temp):
                    os.unlink(temp)
    except OSError as e:
        raise ValueError(f"cannot write {path}: {e.strerror or e}") from e
    rows = len(content.splitlines())
    return "已{} {} 行 / {} 字节：{}".format(
        "追加到" if append else "写入", rows, len(data), path
    )


def file_tools() -> list[AgentTool]:
    """文件这一族：只依赖文件系统，谁都能用。"""
    return [
        AgentTool(READ_FILE_TOOL, read_file),
        AgentTool(WRITE_FILE_TOOL, write_file),
    ]
