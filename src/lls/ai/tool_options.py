"""单个工具的可配置项，以及配置里的形状。

``ToolOptions`` 的每个字段都是三态：``None`` 表示「这一层没说，往下继承」。

现在只消费 :attr:`ToolOptions.enabled`；其余字段先占位，将来加一个字段
只需要「解析 + 一个中间件 + 界面一列」，不用改存储结构。

配置形状（``ai.tools`` 段）与 ``meta.json`` 里的 ``tools`` 共用同一个
解析入口::

    ai:
      tools:
        write_file:
          enabled: false
        bash:
          confirm: true
        read_terminal:
          settings:
            visible_only: true
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional

__all__ = ["FIELDS", "ToolOptions", "parse_tool_options", "tool_options_from_json"]

#: 一个工具当前认识的所有字段；加字段时只改这里 + :class:`ToolOptions`。
FIELDS = ("enabled", "confirm", "timeout", "result_budget", "audit", "settings")

_BOOL_FIELDS = ("enabled", "confirm", "audit")
_NUMBER_FIELDS = ("timeout",)
_INT_FIELDS = ("result_budget",)
_MAPPING_FIELDS = ("settings",)


def _merge_settings(
    base: Optional[Mapping[str, Any]], over: Optional[Mapping[str, Any]]
) -> Optional[Dict[str, Any]]:
    """逐键合并两层的工具特有设置；``None`` 表示这一层没说。"""
    if base is None:
        return dict(over) if over else None
    if over is None:
        return dict(base) or None
    merged: Dict[str, Any] = dict(base)
    for key, value in over.items():
        current = merged.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            merged[key] = _merge_settings(current, value)
        else:
            merged[key] = value
    return merged or None


@dataclass(frozen=True)
class ToolOptions:
    """一个工具的配置；``None`` = 这一层没设置。

    ``settings`` 是工具特有的设置（形状由各工具自己解释，例如
    ``read_terminal`` 的 ``visible_only``）。通用项与会话层叠相同，
    但设置本身逐键合并，避免近层只写一个键就丢掉远层其余键。
    """

    enabled: Optional[bool] = None
    confirm: Optional[bool] = None
    timeout: Optional[float] = None
    result_budget: Optional[int] = None
    audit: Optional[bool] = None
    settings: Optional[Mapping[str, Any]] = None

    def merged_with(self, over: "ToolOptions") -> "ToolOptions":
        """``over`` 是更近的一层：它的非 ``None`` 字段覆盖本层。"""
        picks: Dict[str, Any] = {}
        for name in FIELDS:
            value = getattr(over, name)
            if name == "settings":
                picks[name] = _merge_settings(getattr(self, name), value)
            else:
                picks[name] = value if value is not None else getattr(self, name)
        return ToolOptions(**picks)

    def to_json(self) -> dict:
        """只写显式设置过的字段：``None`` 与空设置不进磁盘。"""
        payload: Dict[str, Any] = {}
        for name in FIELDS:
            value = getattr(self, name)
            if value is None or (name == "settings" and not value):
                continue
            payload[name] = dict(value) if name == "settings" else value
        return payload

    def is_empty(self) -> bool:
        return all(
            getattr(self, name) is None
            or (name == "settings" and not getattr(self, name))
            for name in FIELDS
        )


def _as_bool(value: Any, where: str, notes: Optional[list[str]]) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if notes is not None:
        notes.append("{} must be a boolean; ignored".format(where))
    return None


def _as_float(value: Any, where: str, notes: Optional[list[str]]) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        if notes is not None:
            notes.append("{} must be a number; ignored".format(where))
        return None
    if value < 0:
        if notes is not None:
            notes.append("{} must be non-negative; ignored".format(where))
        return None
    return float(value)


def _as_int(value: Any, where: str, notes: Optional[list[str]]) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        if notes is not None:
            notes.append("{} must be an integer; ignored".format(where))
        return None
    if value < 0:
        if notes is not None:
            notes.append("{} must be non-negative; ignored".format(where))
        return None
    return value


def parse_tool_options(
    value: Any,
    where: str,
    notes: Optional[list[str]] = None,
) -> ToolOptions:
    """把一段配置解析成 :class:`ToolOptions`。

    ``where`` 只用于告警文案（``ai.tools.write_file.enabled`` 这种）。
    未知字段、类型不对的值都只告警并忽略，不会让整份配置起不来。
    """
    if value is None:
        return ToolOptions()
    if not isinstance(value, Mapping):
        if notes is not None:
            notes.append("{} must be a mapping; ignored".format(where))
        return ToolOptions()
    for key in value:
        if key not in FIELDS:
            if notes is not None:
                notes.append("unknown config key: {}.{}".format(where, key))
    picked: Dict[str, Any] = {}
    for name in _BOOL_FIELDS:
        if name in value:
            picked[name] = _as_bool(value.get(name), "{}.{}".format(where, name), notes)
    for name in _NUMBER_FIELDS:
        if name in value:
            picked[name] = _as_float(value.get(name), "{}.{}".format(where, name), notes)
    for name in _INT_FIELDS:
        if name in value:
            picked[name] = _as_int(value.get(name), "{}.{}".format(where, name), notes)
    for name in _MAPPING_FIELDS:
        if name in value:
            item = value.get(name)
            if item is None:
                continue
            if not isinstance(item, Mapping):
                if notes is not None:
                    notes.append("{}.{} must be a mapping; ignored".format(where, name))
                continue
            picked[name] = dict(item) or None
    return ToolOptions(**picked)


def tool_options_from_json(value: Any) -> ToolOptions:
    """``meta.json`` 里的同一形状；坏值静默丢弃（那是用户手改的盘上数据）。

    与配置不同，会话元信息坏了没有告警渠道，也不该让一条会话打不开。
    """
    return parse_tool_options(value, "tools", None)
