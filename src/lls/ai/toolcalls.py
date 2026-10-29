"""工具调用分片的归并 —— provider 只交碎片，由使用方拼完整调用。

线上的工具调用是**分片**到达的：每片只带自己的 ``index``，``id`` / ``name``
通常只在首片出现，``arguments`` 是 JSON 文本的一段（单看某片往往不是合法
JSON）。分片长什么样是各家 wire 的事，而「拼成一次完整调用」是所有调用方
都要做的同一件事，所以 provider 不归并，只把碎片原样交出来
（:class:`~lls.ai.ToolCallDelta`）；拼装放在这里::

    merger = ToolCallMerger()
    async for chunk in provider.chat(...):
        if chunk.kind is ChunkKind.TOOL_CALL_DELTA:
            merger.add(chunk.fragment)
    for call in merger.calls():
        ...

``add`` 是就地累加的，读 :meth:`ToolCallMerger.progress` 可以在流中随时
拿到「正在拼的调用」，用来画进度，不必等流结束。
"""

from __future__ import annotations

from typing import Any, Dict

from .types import ToolCall, ToolCallDelta

__all__ = ["ToolCallMerger", "delta_from", "field"]

# 一个累积中的调用：id / name 整体出现，arguments 逐片拼接。
Slot = Dict[str, str]


def field(obj: Any, name: str, default: Any = None) -> Any:
    """同时支持对象（SDK）与 dict（手写 HTTP）的取值。"""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def delta_from(fragment: Any) -> ToolCallDelta:
    """把一个分片（SDK 对象或 dict）转成 :class:`ToolCallDelta`。

    两种传输层的分片形状一样，只有取值方式不同；provider 各自只调这一行。
    """
    index = field(fragment, "index", 0)
    function = field(fragment, "function")
    name = ""
    arguments = ""
    if function is not None:
        name = field(function, "name") or ""
        arguments = field(function, "arguments") or ""
    return ToolCallDelta(
        index=index if isinstance(index, int) else 0,
        id=field(fragment, "id") or "",
        name=name,
        arguments=arguments,
    )


class ToolCallMerger:
    """按 ``index`` 分槽累加分片。"""

    def __init__(self) -> None:
        self._slots: Dict[int, Slot] = {}

    def __bool__(self) -> bool:
        return bool(self._slots)

    def __len__(self) -> int:
        return len(self._slots)

    def add(self, delta: ToolCallDelta) -> None:
        """归并一个分片。

        ``id`` / ``name`` 是整体出现的：有的服务每片都重复带上完整的 name，
        逐片拼接会拼成 ``send_textsend_text``。所以只有这片的名字与已收到的
        不同时才追加。
        """
        slot = self._slots.get(delta.index)
        if slot is None:
            slot = self._slots[delta.index] = {"id": "", "name": "", "arguments": ""}
        if delta.id:
            slot["id"] = delta.id
        if delta.name and delta.name != slot["name"]:
            slot["name"] += delta.name
        if delta.arguments:
            slot["arguments"] += delta.arguments

    def progress(self) -> tuple[str, str]:
        """``(name, arguments 尾部)`` —— 第一个调用当前的拼装状态。

        给 UI 画进度用；什么都还没收到时返回两个空串。
        """
        for index in sorted(self._slots):
            slot = self._slots[index]
            if slot["name"]:
                return slot["name"], slot["arguments"][-200:]
        return "", ""

    def calls(self) -> list[ToolCall]:
        """流结束后交出完整调用，按 ``index`` 排序。

        没有 name 的残槽（例如只收到一半就断了）直接丢弃：拼出来的调用
        也没法执行。
        """
        calls = []
        for index in sorted(self._slots):
            slot = self._slots[index]
            if not slot["name"]:
                continue
            calls.append(
                ToolCall(
                    id=slot["id"] or "call_%d" % index,
                    name=slot["name"],
                    arguments=slot["arguments"],
                )
            )
        return calls
