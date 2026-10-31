"""工具策略：全局 / 会话两层设置，以及它们的合成与过滤。

一层策略就是 ``{工具名: ToolOptions}``。合成是**逐字段覆盖**：
``会话 -> 全局 -> 代码默认``（``None`` 表示这一层没说，继续往下找）。
没有「安全字段只能收紧」这种特殊规则 —— 哪一层更近就听哪一层的。

过滤发生在装配时：被禁用的工具根本不出现在请求的 ``tools`` 数组里，
模型看不见它，也就不存在「调用被拒」的回合。``Agent`` 一无所知。

全局策略是进程级单例（与 :func:`lls.ai.tools.shell.shared` 同理）：同一
台机器、同一个进程改一次，之后打开的会话都按它算。会话策略由调用方从
``SessionMeta.tools`` 构造；``ToolPolicy.merged(session)`` 得到实际生效值。
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, TYPE_CHECKING

from ..tool_options import ToolOptions, parse_tool_options, tool_options_from_json

if TYPE_CHECKING:  # pragma: no cover - 只是类型
    from ..agent import AgentTool

__all__ = [
    "ToolPolicy",
    "global_policy",
    "reset_global_policy",
    "set_global_policy",
]


class ToolPolicy:
    """一层工具设置；``{name: ToolOptions}``。"""

    def __init__(self, settings: Optional[Mapping[str, ToolOptions]] = None) -> None:
        self._settings: Dict[str, ToolOptions] = {
            str(name): options
            for name, options in (settings or {}).items()
            if not options.is_empty()
        }

    # ---- 读 ----

    def options(self, name: str) -> ToolOptions:
        return self._settings.get(name, ToolOptions())

    def settings(self) -> Dict[str, ToolOptions]:
        """显式设置过的项（副本）。"""
        return dict(self._settings)

    def names(self) -> List[str]:
        return sorted(self._settings)

    def disabled(self) -> List[str]:
        """显式设置成 ``enabled: false`` 的工具名。"""
        return sorted(
            name
            for name, options in self._settings.items()
            if options.enabled is False
        )

    def enabled(self, name: str) -> bool:
        """本层的看法；没设置过就是启用（继承默认）。"""
        return self.options(name).enabled is not False

    def __len__(self) -> int:
        return len(self._settings)

    def __contains__(self, name: object) -> bool:
        return name in self._settings

    def __iter__(self) -> Iterator[str]:
        return iter(self.names())

    # ---- 写 ----

    def set_options(self, name: str, options: ToolOptions) -> None:
        if options.is_empty():
            self._settings.pop(name, None)
        else:
            self._settings[str(name)] = options

    def enable(self, name: str) -> bool:
        self.set_options(name, self.options(name).merged_with(ToolOptions(enabled=True)))
        return True

    def disable(self, name: str) -> bool:
        self.set_options(name, self.options(name).merged_with(ToolOptions(enabled=False)))
        return False

    def toggle(self, name: str) -> bool:
        """翻转并返回新状态。"""
        if self.enabled(name):
            return self.disable(name)
        return self.enable(name)

    def clear(self) -> None:
        self._settings.clear()

    # ---- 合成 ----

    def merged(self, over: "ToolPolicy") -> "ToolPolicy":
        """``over`` 更近：它的显式字段覆盖本层，其余沿用本层。"""
        combined: Dict[str, ToolOptions] = dict(self._settings)
        for name, options in over.settings().items():
            combined[name] = self.options(name).merged_with(options)
        return ToolPolicy(combined)

    # ---- 用 ----

    def filter(self, tools: Iterable["AgentTool"]) -> List["AgentTool"]:
        """按本层设置过滤工具，保持原有顺序（顺序即请求里的顺序）。"""
        return [
            entry for entry in tools if self.options(entry.tool.name).enabled is not False
        ]

    def unknown(self, available: Iterable[str]) -> List[str]:
        """策略里写了、但当前工具集里没有的名字。"""
        known = set(available)
        return [name for name in self.names() if name not in known]

    # ---- 序列化 ----

    def to_json(self) -> Dict[str, dict]:
        return {
            name: options.to_json()
            for name, options in self._settings.items()
            if not options.is_empty()
        }

    @classmethod
    def from_json(cls, raw: Any) -> "ToolPolicy":
        """``meta.json`` 或配置里的 ``tools`` 映射；坏值逐条丢弃。"""
        if not isinstance(raw, Mapping):
            return cls()
        settings: Dict[str, ToolOptions] = {}
        for name, value in raw.items():
            options = tool_options_from_json(value)
            if not options.is_empty():
                settings[str(name)] = options
        return cls(settings)

    @classmethod
    def from_config(cls, raw: Any, notes: Optional[list[str]] = None) -> "ToolPolicy":
        """配置里的 ``ai.tools``：未知字段走告警（``notes``）。"""
        if not isinstance(raw, Mapping):
            if notes is not None:
                notes.append("ai.tools must be a mapping; ignored")
            return cls()
        settings: Dict[str, ToolOptions] = {}
        for name, value in raw.items():
            options = parse_tool_options(value, "ai.tools.{}".format(name), notes)
            if not options.is_empty():
                settings[str(name)] = options
        return cls(settings)


_GLOBAL = ToolPolicy()


def global_policy() -> ToolPolicy:
    """进程级的全局策略。"""
    return _GLOBAL


def set_global_policy(policy: ToolPolicy) -> None:
    global _GLOBAL
    _GLOBAL = policy


def reset_global_policy(settings: Optional[Mapping[str, ToolOptions]] = None) -> None:
    """重建全局策略；测试与配置重载用。"""
    global _GLOBAL
    _GLOBAL = ToolPolicy(settings)
