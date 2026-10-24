"""配置文件读取：YAML + 路径解析。

配置形态参考 opencode：``providers`` 是以 id 为键的映射，每项声明实现
``type`` 与 ``base_url`` / ``api_key`` / ``models`` 等；默认模型用
``provider_id/model_id`` 寻址。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .types import ProviderConfig

__all__ = [
    "AIConfig",
    "ConfigError",
    "DEFAULT_FILENAME",
    "MINIMAL_EXAMPLE",
    "load",
    "resolve_config_path",
]

DEFAULT_FILENAME = "config.yaml"

MINIMAL_EXAMPLE = """\
ai:
  default_model: openai/gpt-4o-mini
  providers:
    openai:
      type: openai
      base_url: https://api.openai.com/v1
      api_key: sk-...
      models: [gpt-4o-mini]"""


class ConfigError(Exception):
    """配置缺失或非法。"""


@dataclass(frozen=True)
class AIConfig:
    """解析后的 ``ai:`` 段。"""

    providers: Dict[str, ProviderConfig] = field(default_factory=dict)
    default_model: Optional[str] = None
    path: Optional[Path] = None
    """配置来源文件。"""


def resolve_config_path(explicit: Optional[str] = None) -> Path:
    """按 ``--config`` > ``LLS_CONFIG`` > ``XDG_CONFIG_HOME`` > ``~/.config`` 定路径。"""
    if explicit:
        return Path(explicit).expanduser()
    from_env = os.environ.get("LLS_CONFIG")
    if from_env:
        return Path(from_env).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".config"
    return root / "lls" / DEFAULT_FILENAME


def _as_str(value: Any, field_name: str, fallback: Optional[str] = None) -> Optional[str]:
    if value is None:
        return fallback
    if not isinstance(value, str):
        raise ConfigError("%s must be a string" % field_name)
    return value


def _parse_provider(pid: str, data: Any) -> ProviderConfig:
    if not isinstance(data, dict):
        raise ConfigError("ai.providers.%s must be a mapping" % pid)
    ptype = _as_str(data.get("type"), "ai.providers.%s.type" % pid)
    if not ptype:
        raise ConfigError("ai.providers.%s.type is required" % pid)

    models_raw = data.get("models") or []
    if not isinstance(models_raw, list):
        raise ConfigError("ai.providers.%s.models must be a list" % pid)
    models = []  # type: List[str]
    for item in models_raw:
        if not isinstance(item, str):
            raise ConfigError("ai.providers.%s.models entries must be strings" % pid)
        models.append(item)

    headers_raw = data.get("headers") or {}
    if not isinstance(headers_raw, dict):
        raise ConfigError("ai.providers.%s.headers must be a mapping" % pid)

    params_raw = data.get("params") or {}
    if not isinstance(params_raw, dict):
        raise ConfigError("ai.providers.%s.params must be a mapping" % pid)

    timeout_raw = data.get("timeout", 60.0)
    try:
        timeout = float(timeout_raw)
    except (TypeError, ValueError) as e:
        raise ConfigError("ai.providers.%s.timeout must be a number" % pid) from e

    return ProviderConfig(
        id=pid,
        type=ptype,
        name=_as_str(data.get("name"), "ai.providers.%s.name" % pid, "") or "",
        base_url=_as_str(data.get("base_url"), "ai.providers.%s.base_url" % pid),
        api_key=_as_str(data.get("api_key"), "ai.providers.%s.api_key" % pid),
        headers={str(k): str(v) for k, v in headers_raw.items()},
        timeout=timeout,
        models=models,
        params=dict(params_raw),
    )


def _parse_ai(data: Dict[str, Any], path: Path) -> AIConfig:
    section = data.get("ai") or {}
    if not isinstance(section, dict):
        raise ConfigError("ai must be a mapping")

    providers_raw = section.get("providers") or {}
    if not isinstance(providers_raw, dict):
        raise ConfigError("ai.providers must be a mapping")
    providers = {
        str(pid): _parse_provider(str(pid), conf)
        for pid, conf in providers_raw.items()
    }

    default_model = _as_str(section.get("default_model"), "ai.default_model")

    return AIConfig(
        providers=providers,
        default_model=default_model,
        path=path,
    )


def load(path=None):
    """读取并解析配置。

    :param path: 显式路径；省略时按 :func:`resolve_config_path` 的顺序解析。
    :raises ConfigError: 文件不存在、YAML 语法错误或字段非法。
    """
    target = Path(path).expanduser() if path is not None else resolve_config_path()
    if not target.exists():
        raise ConfigError(
            "config not found: %s\ncreate it with at least:\n\n%s"
            % (target, MINIMAL_EXAMPLE)
        )
    try:
        import yaml
    except ImportError as e:  # pragma: no cover - 声明为硬依赖
        raise ConfigError("PyYAML is required: pip install pyyaml") from e

    try:
        raw = yaml.safe_load(target.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ConfigError("invalid YAML in %s: %s" % (target, e)) from e

    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError("top-level config must be a mapping")

    return _parse_ai(raw, target.resolve())
