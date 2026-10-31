"""Catalog —— 把 ``provider_id/model_id`` 解析成可用实例。

实例按 provider id 懒创建并缓存；同一个 id 多次请求只构造一次。
"""

from __future__ import annotations

from functools import lru_cache

from ..config import ConfigError, load_ai, resolve_config_path
from .config import AIConfig
from .provider import Provider
from .tool_options import ToolOptions
from .types import ProviderConfig
from .registry import create_provider

__all__ = ["Catalog", "default_catalog", "parse_model_spec"]


def parse_model_spec(spec: str) -> tuple[str, str]:
    """把 ``provider/model`` 拆成 ``(provider_id, model_id)``。

    model id 本身可能包含 ``/``（如 ``meta-llama/llama-3``），因此只按第一个
    斜杠切分。
    """
    provider_id, sep, model_id = spec.partition("/")
    provider_id = provider_id.strip()
    model_id = model_id.strip()
    if not sep or not provider_id or not model_id:
        raise ConfigError(f"model spec must be 'provider/model', got {spec!r}")
    return provider_id, model_id


class Catalog:
    """配置驱动的 provider 实例仓库。"""

    def __init__(self, config: AIConfig) -> None:
        self._config = config
        self._instances: dict[str, Provider] = {}

    def provider_ids(self) -> list[str]:
        return sorted(self._config.providers)

    def get(self, provider_id: str) -> Provider:
        """按 id 取实例，懒创建并缓存。"""
        cached = self._instances.get(provider_id)
        if cached is not None:
            return cached
        conf = self._config.providers.get(provider_id)
        if conf is None:
            raise ConfigError(
                "no such provider {!r} (configured: {})".format(
                        provider_id, ", ".join(self.provider_ids()) or "<none>"
                    )
            )
        instance = create_provider(conf)
        self._instances[provider_id] = instance
        return instance

    def resolve_spec(self, spec: str | None = None) -> tuple[str, str]:
        """把 ``provider/model`` 拆成 ``(provider_id, model_id)``。

        省略时用配置里的 ``default_model``。调用方需要**只**拿到 id 时用它
        （例如记下一条会话用的是哪个 provider），不必为了实例化而构造 provider。

        :raises ConfigError: 无可用配置或 spec 非法。
        """
        chosen = spec or self._config.default_model
        if not chosen:
            raise ConfigError(
                "no model configured: set ai.default_model in %s"
                % (self._config.path or "config")
            )
        return parse_model_spec(chosen)

    def provider_config(self, provider_id: str) -> ProviderConfig:
        """取 provider 的配置（窗口、超时这些预算参数都在这里）。"""
        conf = self._config.providers.get(provider_id)
        if conf is None:
            raise ConfigError("no such provider {!r}".format(provider_id))
        return conf

    def context_limit(self) -> int:
        """配置里的单次请求上限；0 表示不设。"""
        return self._config.context_limit

    def tools(self) -> dict[str, ToolOptions]:
        """``ai.tools`` 段：全局工具设置的初值。"""
        return dict(self._config.tools)

    def resolve(self, spec: str | None = None) -> tuple[Provider, str]:
        """解析 ``provider/model``；省略时用配置里的 ``default_model``。

        :raises ConfigError: 无可用配置、spec 非法或没有该 provider。
        """
        provider_id, model_id = self.resolve_spec(spec)
        return self.get(provider_id), model_id


@lru_cache(maxsize=4)
def _catalog_for(path: str) -> Catalog:
    return Catalog(load_ai(path))


def default_catalog() -> Catalog:
    """按配置路径缓存的默认 :class:`Catalog`。

    同一个配置路径只读一次配置；provider 实例仍由 ``Catalog`` 按 id 懒创建。
    """
    return _catalog_for(str(resolve_config_path()))
