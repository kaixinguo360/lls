"""``learning`` 段：离线数据、训练器、评测与晋升的目录和命令。

主仓库只认识命令和文件契约，不依赖 torch/transformers。训练器怎么写、
用几张卡、产出什么格式，全部由配置里的命令和 ``metrics.json`` 决定。
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

__all__ = ["Command", "LearningConfig", "parse_learning"]


@dataclass(frozen=True)
class Command:
    argv: tuple[str, ...]
    timeout: float = 3600.0


@dataclass(frozen=True)
class LearningConfig:
    root: Path
    runs_dir: Path
    data_dir: Path
    adapters_dir: Path
    train: Optional[Command] = None
    eval: Optional[Command] = None
    eval_tasks: Optional[Path] = None
    baseline: float = 0.0
    min_delta: float = 0.0


def _path(value: Any, base: Path, default: Path) -> Path:
    if not isinstance(value, str) or not value:
        return default
    path = Path(value).expanduser()
    return path if path.is_absolute() else (base / path)


def _float(value: Any, default: float, low: float = 0.0, high: float = 1.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return min(high, max(low, float(value)))


def _command(value: Any, where: str, notes: list[str]) -> Optional[Command]:
    if value is None:
        return None
    argv: list[str]
    timeout = 3600.0
    if isinstance(value, Mapping):
        raw_argv = value.get("command", value.get("argv"))
        timeout = _float(value.get("timeout", 3600.0), 3600.0, 1.0, 86400.0)
        for key in value:
            if key not in ("command", "argv", "timeout"):
                notes.append("未知配置项：{}.{}".format(where, key))
    else:
        raw_argv = value
    if isinstance(raw_argv, str):
        argv = shlex.split(raw_argv)
    elif isinstance(raw_argv, list) and all(
        isinstance(item, str) and item for item in raw_argv
    ):
        argv = list(raw_argv)
    else:
        notes.append("{} 必须是命令列表或字符串；已忽略".format(where))
        return None
    if not argv:
        notes.append("{} 不能为空；已忽略".format(where))
        return None
    return Command(tuple(argv), timeout)


def parse_learning(
    raw: Any,
    *,
    base_dir: Path,
    default_root: Path,
    notes: Optional[list[str]] = None,
) -> LearningConfig:
    """解析顶层 ``learning`` 段；坏值告警并回退，不拖垮配置。"""
    notes = notes if notes is not None else []
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        notes.append("learning 必须是映射；已忽略")
        raw = {}
    allowed = {
        "root",
        "runs_dir",
        "data_dir",
        "adapters_dir",
        "train",
        "eval",
        "baseline",
        "min_delta",
    }
    for key in raw:
        if key not in allowed:
            notes.append("未知配置项：learning.{}".format(key))
    root = _path(raw.get("root"), base_dir, default_root)
    runs_dir = _path(raw.get("runs_dir"), base_dir, root / "runs")
    data_dir = _path(raw.get("data_dir"), base_dir, root / "data")
    adapters_dir = _path(raw.get("adapters_dir"), base_dir, root / "adapters")

    train = _command(raw.get("train"), "learning.train", notes)
    eval_raw = raw.get("eval")
    eval_cmd = None
    eval_tasks = None
    if isinstance(eval_raw, Mapping):
        command_raw = {
            key: value for key, value in eval_raw.items() if key != "tasks"
        }
        eval_cmd = _command(command_raw, "learning.eval", notes)
        eval_tasks = _path(
            eval_raw.get("tasks"), base_dir, root / "eval" / "tasks.jsonl"
        )
    elif eval_raw is not None:
        eval_cmd = _command(eval_raw, "learning.eval", notes)

    return LearningConfig(
        root=root,
        runs_dir=runs_dir,
        data_dir=data_dir,
        adapters_dir=adapters_dir,
        train=train,
        eval=eval_cmd,
        eval_tasks=eval_tasks,
        baseline=_float(raw.get("baseline"), 0.0),
        min_delta=_float(raw.get("min_delta"), 0.0),
    )
