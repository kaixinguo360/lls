"""provider 类型注册表。

``type`` 决定用哪个实现；实现放在 ``providers/`` 目录里，导入即注册。
新增一种后端 = 在 ``providers/`` 加一个模块并调用 :func:`register_type`。
"""

from __future__ import annotations

from typing import Callable

from .provider import Provider
from .types import ProviderConfig

__all__ = [
    "ProviderFactory",
    "available_types",
    "create_provider",
    "register_type",
]

ProviderFactory = Callable[[ProviderConfig], Provider]

_TYPES = {}  # type: Dict[str, ProviderFactory]


def register_type(name: str, factory: ProviderFactory) -> None:
    """注册一种 provider 实现。后注册者覆盖先注册者。"""
    _TYPES[name] = factory


def available_types() -> list[str]:
    """已注册的实现类型名。"""
    return sorted(_TYPES)


def create_provider(config: ProviderConfig) -> Provider:
    """按 ``config.type`` 构造实例。

    :raises KeyError: 类型未注册。
    """
    factory = _TYPES.get(config.type)
    if factory is None:
        raise KeyError(
            "unknown provider type {!r} (registered: {})".format(
                    config.type, ", ".join(available_types()) or "<none>"
                )
        )
    return factory(config)
