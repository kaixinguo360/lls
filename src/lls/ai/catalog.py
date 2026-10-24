"""Catalog —— 把 ``provider_id/model_id`` 解析成可用实例。

实例按 provider id 懒创建并缓存；同一个 id 多次请求只构造一次。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Dict, List, Optional, Tuple

from .config import AIConfig, ConfigError, load, resolve_config_path
from .provider import Provider
from .registry import create_provider

__all__ = ["Catalog", "default_catalog", "parse_model_spec"]


def parse_model_spec(spec: str) -> Tuple[str, str]:
    """把 ``provider/model`` 拆成 ``(provider_id, model_id)``。

    model id 本身可能包含 ``/``（如 ``meta-llama/llama-3``），因此只按第一个
    斜杠切分。
    """
    provider_id, sep, model_id = spec.partition("/")
    provider_id = provider_id.strip()
    model_id = model_id.strip()
    if not sep or not provider_id or not model_id:
        raise ConfigError("model spec must be 'provider/model', got %r" % spec)
    return provider_id, model_id


class Catalog:
    """配置驱动的 provider 实例仓库。"""

    def __init__(self, config: AIConfig) -> None:
        self._config = config
        self._instances: Dict[str, Provider] = {}

    def provider_ids(self) -> List[str]:
        return sorted(self._config.providers)

    def get(self, provider_id: str) -> Provider:
        """按 id 取实例，懒创建并缓存。"""
        cached = self._instances.get(provider_id)
        if cached is not None:
            return cached
        conf = self._config.providers.get(provider_id)
        if conf is None:
            raise ConfigError(
                "no such provider %r (configured: %s)"
                % (provider_id, ", ".join(self.provider_ids()) or "<none>")
            )
        instance = create_provider(conf)
        self._instances[provider_id] = instance
        return instance

    def resolve(self, spec: Optional[str] = None) -> Tuple[Provider, str]:
        """解析 ``provider/model``；省略时用配置里的 ``default_model``。

        :raises ConfigError: 无可用配置或 spec 非法。
        """
        chosen = spec or self._config.default_model
        if not chosen:
            raise ConfigError(
                "no model configured: set ai.default_model in %s"
                % (self._config.path or "config")
            )
        provider_id, model_id = parse_model_spec(chosen)
        return self.get(provider_id), model_id


@lru_cache(maxsize=4)
def _catalog_for(path: str) -> Catalog:
    return Catalog(load(path))


def default_catalog() -> Catalog:
    """按配置路径缓存的默认 :class:`Catalog`。

    同一个配置路径只读一次配置；provider 实例仍由 ``Catalog`` 按 id 懒创建。
    """
    return _catalog_for(str(resolve_config_path()))
