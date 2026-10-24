"""协议里两边共用的部分：流式分片的工具调用归并。

SDK 交出来的是对象，直接发 HTTP 拿到的则是 dict；归并规则完全一样，所以让
两边都走这一份实现，避免各写一遍、各自长歪。
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, Iterator, Optional

from ..types import ToolCall

__all__ = ["field", "merge_tool_fragments", "finished_calls"]

# 一个累积中的调用。id/name 通常只在首片出现，arguments 逐片拼接。
Slot = Dict[str, str]


def field(obj: Any, name: str, default: Any = None) -> Any:
    """同时支持对象与 dict 的取值。"""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def merge_tool_fragments(pending: Dict[int, Slot], fragments: Optional[Iterable[Any]]) -> None:
    """把一次 delta 里的工具调用分片按 ``index`` 归并进 ``pending``。"""
    for fragment in fragments or ():
        index = field(fragment, "index", 0) or 0
        slot = pending.setdefault(index, {"id": "", "name": "", "arguments": ""})
        call_id = field(fragment, "id")
        if call_id:
            slot["id"] = call_id
        function = field(fragment, "function")
        if function is None:
            continue
        name = field(function, "name")
        if name:
            slot["name"] += name
        arguments = field(function, "arguments")
        if arguments:
            slot["arguments"] += arguments


def finished_calls(pending: Dict[int, Slot]) -> Iterator[ToolCall]:
    """流结束后，把归并好的调用按 index 顺序交出来。"""
    for index in sorted(pending):
        slot = pending[index]
        if not slot["name"]:
            continue
        yield ToolCall(
            id=slot["id"] or "call_%d" % index,
            name=slot["name"],
            arguments=slot["arguments"],
        )
