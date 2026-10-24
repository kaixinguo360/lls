"""Catalog 与 model spec 解析的单元测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from lls.ai import AIConfig, Catalog, ConfigError, ProviderConfig, parse_model_spec


def _config(default: str | None = None) -> AIConfig:
    return AIConfig(
        providers={
            "openai": ProviderConfig(
                id="openai",
                type="openai",
                api_key="sk-xxx",
                models=["gpt-4o-mini", "gpt-4o"],
            ),
            "local": ProviderConfig(
                id="local", type="openai", api_key="ollama", models=["qwen3-coder"]
            ),
        },
        default_model=default,
        path=Path("/tmp/config.yaml"),
    )


# ---- spec 解析 ----


def test_parse_simple_spec() -> None:
    assert parse_model_spec("openai/gpt-4o-mini") == ("openai", "gpt-4o-mini")


def test_model_id_may_contain_slash() -> None:
    """只按第一个斜杠切分：模型名本身可能带斜杠。"""
    assert parse_model_spec("gateway/meta-llama/llama-3") == (
        "gateway",
        "meta-llama/llama-3",
    )


def test_spec_strips_whitespace() -> None:
    assert parse_model_spec(" openai / gpt-4o ") == ("openai", "gpt-4o")


@pytest.mark.parametrize("bad", ["", "openai", "/gpt-4o", "openai/"])
def test_parse_rejects_malformed_spec(bad: str) -> None:
    with pytest.raises(ConfigError):
        parse_model_spec(bad)


# ---- 解析默认模型 ----


def test_resolve_uses_default_model() -> None:
    provider, model = Catalog(_config("openai/gpt-4o-mini")).resolve()

    assert provider.id == "openai"
    assert model == "gpt-4o-mini"


def test_resolve_accepts_explicit_spec() -> None:
    provider, model = Catalog(_config("openai/gpt-4o-mini")).resolve("local/qwen3-coder")

    assert provider.id == "local"
    assert model == "qwen3-coder"


def test_resolve_without_default_raises() -> None:
    with pytest.raises(ConfigError) as caught:
        Catalog(_config()).resolve()

    assert "default_model" in str(caught.value)


# ---- 实例缓存 ----


def test_provider_is_created_once() -> None:
    catalog = Catalog(_config("openai/gpt-4o-mini"))

    first = catalog.get("openai")
    second = catalog.get("openai")

    assert first is second


def test_distinct_ids_give_distinct_instances() -> None:
    catalog = Catalog(_config("openai/gpt-4o-mini"))

    assert catalog.get("openai") is not catalog.get("local")


def test_unknown_provider_lists_configured_ids() -> None:
    with pytest.raises(ConfigError) as caught:
        Catalog(_config()).get("nope")

    message = str(caught.value)
    assert "nope" in message
    assert "local" in message and "openai" in message


def test_provider_ids_are_sorted() -> None:
    assert Catalog(_config()).provider_ids() == ["local", "openai"]
