"""应用配置：位置、``paths`` / ``input_history`` / ``input_completion`` / ``ai`` 四段解析。

配置文件是唯一入口，环境变量只负责指定文件位置（``LLS_CONFIG`` /
``XDG_CONFIG_HOME``，以及命令行 ``--config``）。文件不存在、YAML 坏掉或某
一段写错时，靠默认值照常启动；只有真正用到 ``ai`` 的命令才把配置错误报给
用户。未知键会告警，避免把 ``histroy:`` 这种拼写问题静默忽略。
"""

from __future__ import annotations

import copy
import os
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "AppConfig",
    "ConfigError",
    "DEFAULT_FILENAME",
    "DEFAULT_HISTORY_MAX_ENTRIES",
    "DEFAULT_HISTORY_MAX_BYTES",
    "InputCompletionConfig",
    "InputHistoryConfig",
    "MINIMAL_EXAMPLE",
    "PathsConfig",
    "configure",
    "load",
    "load_ai",
    "resolve_config_path",
]

DEFAULT_FILENAME = "config.yaml"

#: 载入进 ↑ 的条数上限。放在这里，配置层与历史层共用同一个默认值。
DEFAULT_HISTORY_MAX_ENTRIES = 200

#: 单个历史文件超过它就自动压缩。
DEFAULT_HISTORY_MAX_BYTES = 128 * 1024

#: 输入行补全的默认长相：行尾虚影（另一种是 ``"redis"`` 轮盘）。
DEFAULT_COMPLETION_STYLE = "ghost"

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
class PathsConfig:
    """``paths`` 段解析后的绝对路径。"""

    root: Path
    sessions_dir: Path
    history_dir: Path


@dataclass(frozen=True)
class InputHistoryConfig:
    """``input_history`` 段的开关与容量。"""

    enabled: bool = True
    max_entries: int = DEFAULT_HISTORY_MAX_ENTRIES
    max_bytes: int = DEFAULT_HISTORY_MAX_BYTES


@dataclass(frozen=True)
class InputCompletionConfig:
    """``input_completion`` 段：Tab 补全的长相。

    ``style`` 只接受 ``"ghost"``（行尾虚影，Tab / → 接受）与 ``"redis"``
    （Tab 轮盘预览）。写错时用默认值并告警。
    """

    style: str = DEFAULT_COMPLETION_STYLE


@dataclass(frozen=True)
class AppConfig:
    """一次配置读取的结果。

    ``ai_error`` 只在 ``ai`` 段缺失/非法时非空：启动仍可用默认值，但 AI
    命令应据此报错。``warnings`` 留给 ``--doctor`` 展示，未知键同时通过
    :mod:`warnings` 发出一次。
    """

    path: Path
    exists: bool
    paths: PathsConfig
    input_history: InputHistoryConfig
    input_completion: InputCompletionConfig
    ai: Any = None
    ai_error: str | None = None
    warnings: tuple[str, ...] = field(default_factory=tuple)


# 命令行参数先于环境变量生效；只影响当前进程。
_CLI_PATH: str | None = None
_CLI_OVERRIDES: tuple[str, ...] = ()


def configure(
    *, path: str | None = None, overrides: Iterable[str] = ()
) -> None:
    """记录命令行给出的 ``--config`` / ``--set``。

    放在模块级而不是层层传参，是为了让 ``ai.sessions``、``catalog`` 这些
    深处模块拿到同一份配置；进程内只在 CLI 入口调用一次。
    """
    global _CLI_PATH, _CLI_OVERRIDES
    parsed = tuple(overrides)
    # 先把语法错误在入口挡掉；否则要等到第一次读配置才报。
    _apply_overrides({}, parsed)
    _CLI_PATH = path
    _CLI_OVERRIDES = parsed


def _reset_for_tests() -> None:
    """清掉进程级覆盖；测试用，不对外承诺。"""
    configure(path=None, overrides=())


def resolve_config_path(explicit: str | None = None) -> Path:
    """按 ``--config`` > ``LLS_CONFIG`` > ``XDG_CONFIG_HOME`` > ``~/.config`` 定路径。"""
    if explicit:
        return Path(explicit).expanduser()
    if _CLI_PATH:
        return Path(_CLI_PATH).expanduser()
    from_env = os.environ.get("LLS_CONFIG")
    if from_env:
        return Path(from_env).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".config"
    return root / "lls" / DEFAULT_FILENAME


def _target(path: str | None) -> Path:
    return resolve_config_path(path).expanduser().resolve()


def _read_raw(target: Path) -> dict[str, Any]:
    """读取 YAML 顶层映射；不存在或坏掉直接抛 :class:`ConfigError`。"""
    if not target.exists():
        raise ConfigError(
            f"未找到配置文件：{target}\n至少需要以下内容：\n\n{MINIMAL_EXAMPLE}"
        )
    try:
        import yaml
    except ImportError as e:  # pragma: no cover - 声明为硬依赖
        raise ConfigError("需要 PyYAML：pip install pyyaml") from e

    try:
        raw = yaml.safe_load(target.read_text(encoding="utf-8"))
    except OSError as e:
        raise ConfigError(f"无法读取 {target}：{e}") from e
    except yaml.YAMLError as e:
        raise ConfigError(f"{target} 中的 YAML 非法：{e}") from e

    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError("配置顶层必须是映射")
    return raw


def _override_value(value: str) -> Any:
    try:
        import yaml
    except ImportError as e:  # pragma: no cover
        raise ConfigError("需要 PyYAML：pip install pyyaml") from e
    try:
        return yaml.safe_load(value)
    except yaml.YAMLError as e:
        raise ConfigError(f"--set 的值非法 {value!r}：{e}") from e


def _apply_overrides(raw: Mapping[str, Any], overrides: Sequence[str]) -> dict[str, Any]:
    """把 ``--set a.b=value`` 合进原始映射，走与 YAML 相同的解析路径。"""
    result = copy.deepcopy(dict(raw))
    for item in overrides:
        key, sep, value = item.partition("=")
        key = key.strip()
        if not sep or not key:
            raise ConfigError(f"--set 需要 KEY=VALUE，得到 {item!r}")
        parts = key.split(".")
        if any(not part for part in parts):
            raise ConfigError(f"--set 的键包含空段：{item!r}")
        node: Any = result
        for part in parts[:-1]:
            child = node.get(part)
            if child is None:
                child = {}
                node[part] = child
            if not isinstance(child, dict):
                raise ConfigError(f"--set 无法进入非映射项 {part!r}")
            node = child
        node[parts[-1]] = _override_value(value)
    return result


def _mapping(raw: Mapping[str, Any], name: str, notes: list[str]) -> dict[str, Any]:
    value = raw.get(name)
    if value is None:
        return {}
    if not isinstance(value, dict):
        notes.append(f"{name} 必须是映射；使用默认值")
        return {}
    return value


#: 改过名的键：老配置写错时给一句“现在叫啥”，而不是只说不知道。
_RENAMED_KEYS = {
    ("", "state"): "paths",
    ("", "history"): "input_history",
    ("paths.", "home"): "root",
    ("paths.", "sessions"): "sessions_dir",
    ("paths.", "history"): "history_dir",
    ("input_history.", "limit"): "max_entries",
}


def _unknown(
    section: Mapping[str, Any], allowed: Iterable[str], prefix: str, notes: list[str]
) -> None:
    known = set(allowed)
    for key in section:
        if key in known:
            continue
        renamed = _RENAMED_KEYS.get((prefix, key))
        if renamed:
            notes.append(f"配置项 {prefix}{key} 已更名为 {prefix}{renamed}")
        else:
            notes.append(f"未知配置项：{prefix}{key}")


def _path_value(
    value: Any,
    field_name: str,
    default: Path,
    base: Path,
    notes: list[str],
) -> Path:
    if value is None:
        return default
    if not isinstance(value, str) or not value.strip():
        notes.append(f"{field_name} 必须是非空路径；使用 {default}")
        return default
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base / path
    return path


def _positive_int(value: Any, field_name: str, default: int, notes: list[str]) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        notes.append(f"{field_name} 必须是正整数；使用 {default}")
        return default
    return value


def _bool_value(value: Any, field_name: str, default: bool, notes: list[str]) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "on", "yes", "true"}:
            return True
        if text in {"0", "off", "no", "false"}:
            return False
    notes.append(f"{field_name} 必须是布尔值；使用 {default}")
    return default


def _parse_paths(
    raw: Mapping[str, Any], target: Path, notes: list[str]
) -> PathsConfig:
    """三个路径共用同一个 base（配置文件所在目录），别各解各的相对路径。"""
    section = _mapping(raw, "paths", notes)
    _unknown(section, {"root", "sessions_dir", "history_dir"}, "paths.", notes)
    base = target.parent
    root = _path_value(
        section.get("root"),
        "paths.root",
        Path.home() / ".local" / "lls",
        base,
        notes,
    )
    sessions_dir = _path_value(
        section.get("sessions_dir"),
        "paths.sessions_dir",
        root / "sessions",
        base,
        notes,
    )
    history_dir = _path_value(
        section.get("history_dir"),
        "paths.history_dir",
        root / "history",
        base,
        notes,
    )
    return PathsConfig(root=root, sessions_dir=sessions_dir, history_dir=history_dir)


def _parse_input_history(
    raw: Mapping[str, Any], notes: list[str]
) -> InputHistoryConfig:
    section = _mapping(raw, "input_history", notes)
    _unknown(section, {"enabled", "max_entries", "max_bytes"}, "input_history.", notes)
    return InputHistoryConfig(
        enabled=_bool_value(
            section.get("enabled"), "input_history.enabled", True, notes
        ),
        max_entries=_positive_int(
            section.get("max_entries"),
            "input_history.max_entries",
            DEFAULT_HISTORY_MAX_ENTRIES,
            notes,
        ),
        max_bytes=_positive_int(
            section.get("max_bytes"),
            "input_history.max_bytes",
            DEFAULT_HISTORY_MAX_BYTES,
            notes,
        ),
    )


def _parse_input_completion(
    raw: Mapping[str, Any], notes: list[str]
) -> InputCompletionConfig:
    section = _mapping(raw, "input_completion", notes)
    _unknown(section, {"style"}, "input_completion.", notes)
    value = section.get("style")
    if value is None:
        return InputCompletionConfig()
    style = value.strip().lower() if isinstance(value, str) else ""
    if style not in ("ghost", "redis"):
        notes.append(
            "input_completion.style 只能为 ghost 或 redis；使用 "
            + DEFAULT_COMPLETION_STYLE
        )
        style = DEFAULT_COMPLETION_STYLE
    return InputCompletionConfig(style=style)


def _emit(notes: Sequence[str]) -> None:
    for note in notes:
        warnings.warn(note, RuntimeWarning, stacklevel=4)


def load(path: str | None = None, *, emit_warnings: bool = True) -> AppConfig:
    """读取应用配置；文件缺失或坏掉时返回内置默认值。

    ``ai`` 段单独解析：它非法时只记录在 ``ai_error``，不影响终端启动。
    这样普通命令不会因为一份写坏的 AI 配置而起不来。
    """
    target = _target(path)
    notes: list[str] = []
    exists = target.exists()
    raw: Mapping[str, Any] = {}
    ai_error: str | None = None
    if not exists:
        ai_error = (
            f"未找到配置文件：{target}\n至少需要以下内容：\n\n{MINIMAL_EXAMPLE}"
        )
    else:
        try:
            raw = _read_raw(target)
        except ConfigError as e:
            ai_error = str(e)
            raw = {}

    raw = _apply_overrides(raw, _CLI_OVERRIDES)
    _unknown(raw, {"paths", "input_history", "input_completion", "ai"}, "", notes)
    paths = _parse_paths(raw, target, notes)
    input_history = _parse_input_history(raw, notes)
    input_completion = _parse_input_completion(raw, notes)
    ai = None
    if ai_error is None:
        try:
            from .ai.config import parse_ai

            ai = parse_ai(raw, target, notes)
        except ConfigError as e:
            ai_error = str(e)
    if ai is None:
        from .ai.config import AIConfig

        ai = AIConfig(providers={}, default_model=None, path=target)
    if notes and emit_warnings:
        _emit(notes)
    return AppConfig(
        path=target,
        exists=exists,
        paths=paths,
        input_history=input_history,
        input_completion=input_completion,
        ai=ai,
        ai_error=ai_error,
        warnings=tuple(notes),
    )


def load_ai(path: str | None = None, *, emit_warnings: bool = True) -> Any:
    """只解析 ``ai`` 段；缺失或非法时抛 :class:`ConfigError`。

    ``agent`` / ``generate`` 用这个入口，保留原来“没有配置就报错”的行为。
    """
    target = _target(path)
    notes: list[str] = []
    raw = _read_raw(target)
    raw = _apply_overrides(raw, _CLI_OVERRIDES)
    _unknown(raw, {"paths", "input_history", "input_completion", "ai"}, "", notes)
    from .ai.config import parse_ai

    try:
        return parse_ai(raw, target, notes)
    finally:
        if notes and emit_warnings:
            _emit(notes)
