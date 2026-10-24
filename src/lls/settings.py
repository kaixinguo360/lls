"""配置与路径。

类型化配置 + 校验 + 原子写 + 版本迁移。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lls.ai import AIConfig, DEFAULT_ROLES, Role

__all__ = ["Settings", "Paths", "load_settings", "default_settings"]


def _xdg_config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    if base:
        return Path(base) / "lls"
    return Path.home() / ".config" / "lls"


@dataclass(frozen=True, slots=True)
class Settings:
    """LLS 全部配置。"""

    ai: AIConfig
    roles: dict[str, Role]
    role_id: str = "chat"

    def role(self) -> Role:
        from lls.ai import get_role

        return get_role(self.role_id, self.roles)


@dataclass(slots=True)
class Paths:
    config_dir: Path

    @property
    def config_file(self) -> Path:
        return self.config_dir / "config.toml"

    @property
    def history_file(self) -> Path:
        return self.config_dir / "history.jsonl"

    @property
    def screen_log(self) -> Path:
        return self.config_dir / "screen.log"

    @property
    def audit_file(self) -> Path:
        return self.config_dir / "audit.jsonl"


def default_settings() -> Settings:
    return Settings(ai=AIConfig(), roles=dict(DEFAULT_ROLES), role_id="chat")


def _parse_role(role_id: str, data: dict[str, Any]) -> Role:
    base = DEFAULT_ROLES.get(role_id, DEFAULT_ROLES["chat"])
    return Role(
        id=role_id,
        system_prompt=data.get("system_prompt", base.system_prompt),
        user_template=data.get("user_template", base.user_template),
        model=data.get("model", base.model),
        parsers=tuple(data.get("parsers", base.parsers)),
        context_lines=int(data.get("context_lines", base.context_lines)),
        include_cwd=bool(data.get("include_cwd", base.include_cwd)),
        include_last_command=bool(data.get("include_last_command", base.include_last_command)),
    )


def load_settings(path: Path) -> Settings:
    """加载 TOML 配置；文件不存在时返回默认。"""
    if not path.exists():
        return default_settings()

    try:
        import tomli as tomllib  # type: ignore
    except ImportError:  # pragma: no cover
        import tomllib  # type: ignore

    with open(path, "rb") as f:
        data = tomllib.load(f)

    ai_data = data.get("ai", {})
    ai = AIConfig(
        model=ai_data.get("model", "gpt-4o-mini"),
        base_url=ai_data.get("base_url"),
        api_key_env=ai_data.get("api_key_env", "LLS_OPENAI_API_KEY"),
        timeout=float(ai_data.get("timeout", 30.0)),
        params=dict(ai_data.get("params", {})),
    )

    roles = dict(DEFAULT_ROLES)
    for role_id, role_data in data.get("ai", {}).get("roles", {}).items():
        roles[role_id] = _parse_role(role_id, role_data)

    role_id = data.get("ai", {}).get("role", "chat")
    return Settings(ai=ai, roles=roles, role_id=role_id)
