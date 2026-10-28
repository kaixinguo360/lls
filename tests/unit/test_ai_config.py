"""配置文件读取的单元测试（YAML + 路径解析 + 字段校验）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from lls.config import ConfigError, DEFAULT_FILENAME, load_ai as load, resolve_config_path

VALID = """\
ai:
  default_model: openai/gpt-4o-mini
  providers:
    openai:
      type: openai
      name: OpenAI
      base_url: https://api.openai.com/v1
      api_key: sk-xxx
      timeout: 30
      headers:
        X-Org: team
      models: [gpt-4o-mini, gpt-4o]
      params:
        temperature: 0.2
    local:
      type: openai
      base_url: http://localhost:11434/v1
      api_key: ollama
      models: [qwen3-coder]
"""


def _write(tmp_path: Path, text: str, name: str = "config.yaml") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


# ---- 读取 ----


def test_load_reads_providers(tmp_path: Path) -> None:
    config = load(_write(tmp_path, VALID))

    assert sorted(config.providers) == ["local", "openai"]
    assert config.default_model == "openai/gpt-4o-mini"
    assert config.path == _write(tmp_path, VALID).resolve()


def test_load_reads_provider_fields(tmp_path: Path) -> None:
    config = load(_write(tmp_path, VALID))
    openai = config.providers["openai"]

    assert openai.id == "openai"
    assert openai.type == "openai"
    assert openai.name == "OpenAI"
    assert openai.base_url == "https://api.openai.com/v1"
    assert openai.api_key == "sk-xxx"
    assert openai.timeout == 30.0
    assert openai.headers == {"X-Org": "team"}
    assert openai.models == ["gpt-4o-mini", "gpt-4o"]
    assert openai.params == {"temperature": 0.2}


def test_provider_defaults(tmp_path: Path) -> None:
    text = "ai:\n  providers:\n    p:\n      type: openai\n"
    provider = load(_write(tmp_path, text)).providers["p"]

    assert provider.name == ""
    assert provider.base_url is None
    assert provider.api_key is None
    assert provider.headers == {}
    assert provider.models == []
    assert provider.params == {}
    assert provider.timeout == 60.0


def test_absent_ai_section_yields_empty_config(tmp_path: Path) -> None:
    config = load(_write(tmp_path, "other: 1\n"), emit_warnings=False)

    assert config.providers == {}
    assert config.default_model is None


def test_empty_file_is_accepted(tmp_path: Path) -> None:
    config = load(_write(tmp_path, ""))

    assert config.providers == {}


# ---- 错误 ----


def test_missing_file_names_the_path(tmp_path: Path) -> None:
    missing = tmp_path / "nope.yaml"

    with pytest.raises(ConfigError) as caught:
        load(missing)

    assert str(missing) in str(caught.value)
    assert "providers" in str(caught.value)  # 附带最小示例


def test_invalid_yaml_reports_location(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as caught:
        load(_write(tmp_path, "ai: [\n"))

    assert "invalid YAML" in str(caught.value)


def test_top_level_must_be_mapping(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load(_write(tmp_path, "- a\n- b\n"))


def test_provider_must_be_mapping(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as caught:
        load(_write(tmp_path, "ai:\n  providers:\n    p: 1\n"))

    assert "ai.providers.p" in str(caught.value)


def test_type_is_required(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as caught:
        load(_write(tmp_path, "ai:\n  providers:\n    p:\n      api_key: k\n"))

    assert "type is required" in str(caught.value)


def test_models_must_be_list_of_strings(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load(_write(tmp_path, "ai:\n  providers:\n    p:\n      type: openai\n      models: 1\n"))

    with pytest.raises(ConfigError):
        load(
            _write(
                tmp_path,
                "ai:\n  providers:\n    p:\n      type: openai\n      models: [1]\n",
            )
        )


def test_timeout_must_be_number(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as caught:
        load(
            _write(
                tmp_path,
                "ai:\n  providers:\n    p:\n      type: openai\n      timeout: soon\n",
            )
        )

    assert "timeout" in str(caught.value)


# ---- 路径解析 ----


def test_explicit_path_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLS_CONFIG", "/from/env.yaml")

    assert resolve_config_path("/explicit.yaml") == Path("/explicit.yaml")


def test_env_var_beats_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLS_CONFIG", "/from/env.yaml")
    monkeypatch.setenv("XDG_CONFIG_HOME", "/xdg")

    assert resolve_config_path() == Path("/from/env.yaml")


def test_xdg_beats_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLS_CONFIG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", "/xdg")

    assert resolve_config_path() == Path("/xdg/lls") / DEFAULT_FILENAME


def test_falls_back_to_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLS_CONFIG", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    assert resolve_config_path() == Path.home() / ".config" / "lls" / DEFAULT_FILENAME
