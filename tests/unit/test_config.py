"""应用配置：默认值、分段容错、路径派生与 --set 覆盖。"""

from __future__ import annotations

from pathlib import Path

import pytest

from lls.config import ConfigError, configure, load, load_ai


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _reset_cli_config():
    yield
    configure()


def test_missing_file_uses_defaults(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    cfg = load(tmp_path / "missing.yaml", emit_warnings=False)

    assert cfg.exists is False
    assert cfg.paths.root == tmp_path / ".local" / "lls"
    assert cfg.paths.sessions_dir == tmp_path / ".local" / "lls" / "sessions"
    assert cfg.paths.history_dir == tmp_path / ".local" / "lls" / "history"
    assert cfg.input_history.enabled is True
    assert cfg.ai.providers == {}
    assert cfg.ai_error and "config not found" in cfg.ai_error


def test_broken_yaml_uses_defaults_and_ai_still_errors(tmp_path: Path) -> None:
    path = _write(tmp_path, "ai: [\n")

    cfg = load(path, emit_warnings=False)
    assert cfg.paths.root.name == "lls"
    assert cfg.ai_error and "invalid YAML" in cfg.ai_error

    with pytest.raises(ConfigError, match="invalid YAML"):
        load_ai(path)


def test_bad_paths_do_not_hide_history_or_ai(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "paths: [bad]\n"
        "input_history:\n  max_entries: 7\n"
        "ai:\n  default_model: p/m\n  providers:\n    p:\n      type: openai\n",
    )

    cfg = load(path, emit_warnings=False)

    assert cfg.paths.root.name == "lls"
    assert cfg.input_history.max_entries == 7
    assert cfg.ai.default_model == "p/m"
    assert any("paths must be a mapping" in note for note in cfg.warnings)


def test_paths_derive_and_share_one_base(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "paths:\n  root: {}\n  sessions_dir: s\n  history_dir: {}\n".format(
            tmp_path / "base", tmp_path / "elsewhere"
        )
        + "input_history:\n  enabled: false\n  max_entries: 9\n  max_bytes: 123\n",
    )

    cfg = load(path, emit_warnings=False)

    assert cfg.paths.root == tmp_path / "base"
    # 三个路径共用配置文件所在目录做 base，不各自找参照。
    assert cfg.paths.sessions_dir == tmp_path / "s"
    assert cfg.paths.history_dir == tmp_path / "elsewhere"
    assert (
        cfg.input_history.enabled,
        cfg.input_history.max_entries,
        cfg.input_history.max_bytes,
    ) == (False, 9, 123)


def test_unknown_keys_warn_with_full_path(tmp_path: Path) -> None:
    path = _write(
        tmp_path, "paths:\n  rooot: /tmp/x\ninput_history:\n  max_entrie: 5\n"
    )

    with pytest.warns(RuntimeWarning) as caught:
        cfg = load(path)

    messages = [str(item.message) for item in caught]
    assert any("paths.rooot" in message for message in messages)
    assert any("input_history.max_entrie" in message for message in messages)
    assert any("paths.rooot" in note for note in cfg.warnings)


def test_renamed_keys_point_at_the_new_name(tmp_path: Path) -> None:
    path = _write(tmp_path, "state:\n  home: /tmp/x\nhistory:\n  limit: 5\n")

    cfg = load(path, emit_warnings=False)

    notes = "\n".join(cfg.warnings)
    assert "state was renamed to paths" in notes
    assert "history was renamed to input_history" in notes


def test_cli_overrides_use_the_same_parser(tmp_path: Path) -> None:
    path = _write(tmp_path, "ai:\n  providers: {}\n")
    configure(
        path=str(path),
        overrides=[
            "paths.root=/tmp/data-root",
            "input_history.max_entries=3",
            "ai.default_model=p/m",
        ],
    )

    cfg = load(emit_warnings=False)

    assert cfg.paths.root == Path("/tmp/data-root")
    assert cfg.paths.sessions_dir == Path("/tmp/data-root") / "sessions"
    assert cfg.input_history.max_entries == 3
    assert cfg.ai.default_model == "p/m"


def test_bad_override_is_rejected_at_entry() -> None:
    with pytest.raises(ConfigError, match="KEY=VALUE"):
        configure(overrides=["input_history.max_entries"])

def test_completion_style_defaults_to_ghost(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    cfg = load(tmp_path / "missing.yaml", emit_warnings=False)
    assert cfg.input_completion.style == "ghost"


def test_completion_style_accepts_redis_and_rejects_typos(tmp_path: Path) -> None:
    path = _write(tmp_path, "input_completion:\n  style: redis\n")
    assert load(path, emit_warnings=False).input_completion.style == "redis"

    bad = _write(tmp_path, "input_completion:\n  style: fuzzy\n")
    cfg = load(bad, emit_warnings=False)
    assert cfg.input_completion.style == "ghost"
    assert any("input_completion.style" in note for note in cfg.warnings)