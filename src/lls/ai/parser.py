"""输出解析器：把流式正文变成可执行的命令字符串。

解析是 Role 的策略，不是 transport 的职责。纯函数组合，可测试、可配置。
"""

from __future__ import annotations

import json
from typing import Callable

from .types import Chunk, ChunkKind

__all__ = [
    "PARSERS",
    "first_line",
    "strip_fences",
    "strip_quotes",
    "strip_think",
    "parse_json",
]


def strip_think(text: str) -> str:
    """剥掉 ``<think>...</think>``，保留正文。"""
    if "<think>" in text:
        text = text.replace("<think>", "").split("</think>")[-1]
    return text.strip()


def strip_fences(text: str) -> str:
    """剥掉 ```...``` 代码围栏。"""
    t = text.strip()
    if t.startswith("```"):
        t = t[3:]
        if t.endswith("```"):
            t = t[:-3]
        # 去掉首行的语言标记（如 ```bash）
        nl = t.find("\n")
        if nl != -1 and not t[:nl].strip():
            pass
        elif nl != -1 and t[:nl].strip() and all(
            c.isalnum() or c in "-_" for c in t[:nl].strip()
        ):
            t = t[nl + 1 :]
    return t.strip()


def strip_quotes(text: str) -> str:
    """去掉包裹整条命令的引号。"""
    t = text.strip()
    if len(t) >= 2 and t[0] == t[-1] and t[0] in ("'", '"', "`"):
        return t[1:-1]
    return t


def first_line(text: str) -> str:
    """只取第一行。"""
    return text.split("\n", 1)[0].strip()


def parse_json(text: str) -> str:
    """解析 JSON 输出，取 ``command`` 字段。"""
    t = text.strip()
    if t.startswith("```"):
        t = strip_fences(t)
    try:
        obj = json.loads(t)
    except json.JSONDecodeError:
        # 尝试截取第一个 { 到最后一个 }
        i, j = t.find("{"), t.rfind("}")
        if i == -1 or j == -1:
            return t
        obj = json.loads(t[i : j + 1])
    if isinstance(obj, dict):
        return str(obj.get("command", "")).strip()
    return t


ParserFn = Callable[[str], str]

PARSERS: dict[str, ParserFn] = {
    "strip_think": strip_think,
    "strip_fences": strip_fences,
    "strip_quotes": strip_quotes,
    "first_line": first_line,
    "json": parse_json,
}


def apply_parsers(text: str, names: list[str]) -> str:
    """按顺序应用解析器链。"""
    out = text
    for name in names:
        fn = PARSERS.get(name)
        if fn is not None:
            out = fn(out)
    return out


def chunks_to_text(chunks: list[Chunk]) -> str:
    """从 chunk 列表提取正文文本。"""
    return "".join(c.text for c in chunks if c.kind == ChunkKind.TEXT)
