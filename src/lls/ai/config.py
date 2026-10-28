"""``ai`` 段解析：只认识 ``ai:`` 下面的形状，不负责找文件或读 YAML。

应用配置的入口在 :mod:`lls.config`；这里保留纯函数 :func:`parse_ai`，方便
单独测试 provider 字段校验，也避免 AI 模块反过来依赖应用状态。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

from ..config import ConfigError
from .types import ProviderConfig

__all__ = ["AIConfig", "parse_ai"]


@dataclass(frozen=True)
class AIConfig:
    """解析后的 ``ai:`` 段。"""

    providers: dict[str, ProviderConfig] = field(default_factory=dict)
    default_model: str | None = None
    path: Path | None = None
    """配置来源文件。"""


def _as_str(value: Any, field_name: str, fallback: str | None = None) -> str | None:
    if value is None:
        return fallback
    if not isinstance(value, str):
        raise ConfigError(f"{field_name} must be a string")
    return value


def _unknown(
    section: Mapping[str, Any], allowed: set[str], prefix: str, notes: list[str] | None
) -> None:
    if notes is None:
        return
    for key in section:
        if key not in allowed:
            notes.append(f"unknown config key: {prefix}{key}")


def _parse_provider(
    pid: str, data: Any, notes: Optional[list[str]] = None
) -> ProviderConfig:
    if not isinstance(data, dict):
        raise ConfigError(f"ai.providers.{pid} must be a mapping")
    _unknown(
        data,
        {
            "type",
            "name",
            "base_url",
            "api_key",
            "headers",
            "timeout",
            "models",
            "params",
        },
        f"ai.providers.{pid}.",
        notes,
    )
    ptype = _as_str(data.get("type"), f"ai.providers.{pid}.type")
    if not ptype:
        raise ConfigError(f"ai.providers.{pid}.type is required")

    models_raw = data.get("models") or []
    if not isinstance(models_raw, list):
        raise ConfigError(f"ai.providers.{pid}.models must be a list")
    models: list[str] = []
    for item in models_raw:
        if not isinstance(item, str):
            raise ConfigError(f"ai.providers.{pid}.models entries must be strings")
        models.append(item)

    headers_raw = data.get("headers") or {}
    if not isinstance(headers_raw, dict):
        raise ConfigError(f"ai.providers.{pid}.headers must be a mapping")

    params_raw = data.get("params") or {}
    if not isinstance(params_raw, dict):
        raise ConfigError(f"ai.providers.{pid}.params must be a mapping")

    timeout_raw = data.get("timeout", 60.0)
    try:
        timeout = float(timeout_raw)
    except (TypeError, ValueError) as e:
        raise ConfigError(f"ai.providers.{pid}.timeout must be a number") from e

    return ProviderConfig(
        id=pid,
        type=ptype,
        name=_as_str(data.get("name"), f"ai.providers.{pid}.name", "") or "",
        base_url=_as_str(data.get("base_url"), f"ai.providers.{pid}.base_url"),
        api_key=_as_str(data.get("api_key"), f"ai.providers.{pid}.api_key"),
        headers={str(k): str(v) for k, v in headers_raw.items()},
        timeout=timeout,
        models=models,
        params=dict(params_raw),
    )


def parse_ai(
    data: Mapping[str, Any],
    path: Path,
    notes: Optional[list[str]] = None,
) -> AIConfig:
    """解析顶层映射里的 ``ai`` 段。

    :param notes: 收集未知键等告警；``None`` 表示不收集。
    :raises ConfigError: ``ai`` 或其 provider 字段非法。
    """
    section = data.get("ai") or {}
    if not isinstance(section, dict):
        raise ConfigError("ai must be a mapping")
    _unknown(section, {"default_model", "providers"}, "ai.", notes)

    providers_raw = section.get("providers") or {}
    if not isinstance(providers_raw, dict):
        raise ConfigError("ai.providers must be a mapping")
    providers = {
        str(pid): _parse_provider(str(pid), conf, notes)
        for pid, conf in providers_raw.items()
    }

    default_model = _as_str(section.get("default_model"), "ai.default_model")

    return AIConfig(
        providers=providers,
        default_model=default_model,
        path=path,
    )
