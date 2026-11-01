"""``learning`` 段：教师求助的开关、预算与脱敏规则。

只在用到 ``learn`` 的地方解析，不拖慢普通终端启动；未知键只告警，
坏值回退默认值，和 :mod:`lls.config` 的其他段保持一致。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, List, Mapping, Optional

__all__ = ["HelpConfig", "LearningConfig", "parse_learning"]


@dataclass(frozen=True)
class HelpConfig:
    """``learning.help``：要不要把上下文发给教师，以及发多少。"""

    enabled: bool = False
    teacher_model: str = ""
    max_per_turn: int = 2
    max_per_session: int = 10
    min_tool_calls: int = 2
    timeout: float = 60.0
    max_chars: int = 1200
    redact: tuple[str, ...] = ()


@dataclass(frozen=True)
class LearningConfig:
    help: HelpConfig = field(default_factory=HelpConfig)


def _bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    return default


def _positive(value: Any, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return default
    return value


def _text(value: Any, default: str) -> str:
    return value if isinstance(value, str) else default


def _rules(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item)


def parse_learning(
    raw: Any,
    notes: Optional[List[str]] = None,
) -> LearningConfig:
    """解析顶层映射里的 ``learning`` 段；坏值只告警，不抛错。"""
    if raw is None:
        return LearningConfig()
    if not isinstance(raw, Mapping):
        if notes is not None:
            notes.append("learning 必须是映射；已忽略")
        return LearningConfig()
    for key in raw:
        if key not in ("help",):
            if notes is not None:
                notes.append("未知配置项：learning.{}".format(key))
    help_raw = raw.get("help")
    if help_raw is None:
        return LearningConfig()
    if not isinstance(help_raw, Mapping):
        if notes is not None:
            notes.append("learning.help 必须是映射；已忽略")
        return LearningConfig()
    allowed = {
        "enabled",
        "teacher_model",
        "max_per_turn",
        "max_per_session",
        "min_tool_calls",
        "timeout",
        "max_chars",
        "redact",
    }
    for key in help_raw:
        if key not in allowed:
            if notes is not None:
                notes.append("未知配置项：learning.help.{}".format(key))
    timeout = help_raw.get("timeout", 60.0)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        if notes is not None:
            notes.append("learning.help.timeout 必须是数字；使用 60")
        timeout = 60.0
    help_config = HelpConfig(
        enabled=_bool(help_raw.get("enabled"), False),
        teacher_model=_text(help_raw.get("teacher_model"), ""),
        max_per_turn=_positive(help_raw.get("max_per_turn"), 2),
        max_per_session=_positive(help_raw.get("max_per_session"), 10),
        min_tool_calls=_positive(help_raw.get("min_tool_calls"), 2),
        timeout=float(timeout),
        max_chars=_positive(help_raw.get("max_chars"), 1200),
        redact=_rules(help_raw.get("redact")),
    )
    return LearningConfig(help=help_config)


def redact_patterns(config: LearningConfig) -> Iterable[str]:
    """``redact`` 为空时由 :func:`lls.learn.redact.redact` 用内置规则。"""
    return config.help.redact
