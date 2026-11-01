"""离线后训练流水线：导出 → 训练 → 评测 → 晋升。

正向推理进程从不 import 本模块；这里只读写磁盘、起子进程。训练器与评测器
是外部命令，通过占位符与 ``LLS_*`` 环境变量拿到路径；主仓库不依赖 torch。
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

from .config import Command, LearningConfig
from .dataset import MANIFEST, SAMPLES, export

__all__ = [
    "EvalError",
    "GateError",
    "PipelineError",
    "Run",
    "TrainError",
    "create_run",
    "current",
    "doctor",
    "evaluate",
    "export_into_run",
    "gate",
    "promote",
    "train",
    "cycle",
]

RUN_FILE = "run.json"
TRAIN_FILE = "train.json"
EVAL_FILE = "eval.json"
PROMOTE_FILE = "promote.json"
CYCLE_FILE = "cycle.json"
CURRENT = "current.json"
HISTORY = "history.jsonl"


class PipelineError(RuntimeError):
    """流水线失败；消息可直接给用户看。"""


class TrainError(PipelineError):
    pass


class EvalError(PipelineError):
    pass


class GateError(PipelineError):
    pass


@dataclass(frozen=True)
class Run:
    root: Path

    @property
    def id(self) -> str:
        return self.root.name

    @property
    def samples(self) -> Path:
        return self.root / SAMPLES

    @property
    def manifest(self) -> Path:
        return self.root / MANIFEST

    @property
    def adapter(self) -> Path:
        return self.root / "adapter"

    @property
    def train_log(self) -> Path:
        return self.root / "train.log"

    @property
    def eval_log(self) -> Path:
        return self.root / "eval.log"

    @property
    def metrics(self) -> Path:
        return self.root / EVAL_FILE


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as e:
        raise PipelineError("读不到 {}：{}".format(path, e)) from e
    except ValueError as e:
        raise PipelineError("{} 不是合法 JSON：{}".format(path, e)) from e
    if not isinstance(data, dict):
        raise PipelineError("{} 必须是 JSON 对象".format(path))
    return data


def _write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(tmp, path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _new_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)


def create_run(config: LearningConfig, run_id: Optional[str] = None) -> Run:
    run = Run(config.runs_dir / (run_id or _new_id()))
    run.root.mkdir(parents=True, exist_ok=False)
    _write_json(
        run.root / RUN_FILE,
        {
            "id": run.id,
            "created_at": time.time(),
            "learning_root": str(config.root),
        },
    )
    return run


def _placeholders(config: LearningConfig, run: Run, metrics: Path) -> Dict[str, str]:
    return {
        "run": str(run.root),
        "dataset": str(run.root),
        "sft": str(run.samples),
        "manifest": str(run.manifest),
        "adapter": str(run.adapter),
        "metrics": str(metrics),
        "tasks": str(config.eval_tasks) if config.eval_tasks else "",
        "root": str(config.root),
        "data": str(config.data_dir),
        "adapters": str(config.adapters_dir),
    }


def _expand(argv: Sequence[str], values: Dict[str, str]) -> list[str]:
    rows: list[str] = []
    for item in argv:
        try:
            rows.append(item.format(**values))
        except KeyError as e:
            raise PipelineError("命令占位符不存在：{}".format(e)) from e
    return rows


def _run_command(
    command: Command,
    argv: Sequence[str],
    run: Run,
    log: Path,
    *,
    env_extra: Dict[str, str],
) -> Dict[str, Any]:
    env = os.environ.copy()
    env.update(env_extra)
    started = time.time()
    try:
        completed = subprocess.run(
            list(argv),
            cwd=str(run.root),
            env=env,
            capture_output=True,
            text=True,
            timeout=command.timeout,
            check=False,
        )
    except FileNotFoundError as e:
        raise PipelineError("命令不存在：{}".format(argv[0])) from e
    except subprocess.TimeoutExpired as e:
        raise PipelineError("命令超时（{}s）：{}".format(command.timeout, argv)) from e
    elapsed = time.time() - started
    log.write_text(
        "$ {}\n\n[stdout]\n{}\n[stderr]\n{}\n".format(
            " ".join(argv), completed.stdout, completed.stderr
        ),
        encoding="utf-8",
    )
    result = {
        "command": list(argv),
        "returncode": completed.returncode,
        "duration": elapsed,
    }
    if completed.returncode != 0:
        tail = (completed.stderr or completed.stdout or "").strip().splitlines()[-8:]
        raise PipelineError(
            "命令失败（{}）：{}\n{}".format(completed.returncode, argv, "\n".join(tail))
        )
    return result


def _verify_dataset(run: Run) -> Dict[str, Any]:
    if not run.samples.is_file() or not run.manifest.is_file():
        raise PipelineError("运行目录缺少 sft.jsonl/manifest.json：{}".format(run.root))
    manifest = _read_json(run.manifest)
    if not isinstance(manifest.get("samples"), int) or manifest["samples"] <= 0:
        raise PipelineError("数据集没有样本：{}".format(run.manifest))
    expected = manifest.get("sha256")
    if expected and _sha256(run.samples) != expected:
        raise PipelineError("sft.jsonl 与 manifest 的 sha256 不一致")
    return manifest


def export_into_run(
    config: LearningConfig,
    run: Run,
    sessions_root: Path,
    *,
    include_failed: bool = False,
) -> Dict[str, Any]:
    return export(sessions_root, run.root, include_failed=include_failed)


def train(config: LearningConfig, run: Run) -> Dict[str, Any]:
    if config.train is None:
        raise TrainError("未配置 learning.train.command")
    _verify_dataset(run)
    run.adapter.mkdir(parents=True, exist_ok=True)
    values = _placeholders(config, run, run.metrics)
    argv = _expand(config.train.argv, values)
    result = _run_command(
        config.train,
        argv,
        run,
        run.train_log,
        env_extra={
            "LLS_RUN": str(run.root),
            "LLS_DATASET": str(run.root),
            "LLS_SFT": str(run.samples),
            "LLS_MANIFEST": str(run.manifest),
            "LLS_ADAPTER": str(run.adapter),
            "LLS_METRICS": str(run.metrics),
        },
    )
    if not run.adapter.is_dir() or not any(run.adapter.iterdir()):
        raise TrainError("训练器没有在 {} 写出 adapter".format(run.adapter))
    payload = {"status": "ok", "adapter": str(run.adapter), **result}
    _write_json(run.root / TRAIN_FILE, payload)
    return payload


def _metrics(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        raise EvalError("评测器没有写出 {}".format(path))
    data = _read_json(path)
    score = data.get("score")
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        raise EvalError("eval.json.score 必须是 0..1 的数字")
    score = float(score)
    if not 0.0 <= score <= 1.0:
        raise EvalError("eval.json.score 必须在 0..1 之间")
    if "passed" in data and not isinstance(data["passed"], int):
        raise EvalError("eval.json.passed 必须是整数")
    if "total" in data and not isinstance(data["total"], int):
        raise EvalError("eval.json.total 必须是整数")
    return data


def evaluate(config: LearningConfig, run: Run) -> Dict[str, Any]:
    if config.eval is None:
        raise EvalError("未配置 learning.eval.command")
    if not run.adapter.is_dir():
        raise EvalError("运行目录没有 adapter：{}".format(run.adapter))
    values = _placeholders(config, run, run.metrics)
    argv = _expand(config.eval.argv, values)
    if run.metrics.exists():
        run.metrics.unlink()
    result = _run_command(
        config.eval,
        argv,
        run,
        run.eval_log,
        env_extra={
            "LLS_RUN": str(run.root),
            "LLS_ADAPTER": str(run.adapter),
            "LLS_METRICS": str(run.metrics),
            "LLS_TASKS": values["tasks"],
        },
    )
    data = _metrics(run.metrics)
    payload = {"score": float(data["score"]), **data, **result}
    _write_json(run.metrics, payload)
    return payload


def _baseline(config: LearningConfig) -> float:
    score = config.baseline
    current_file = config.adapters_dir / CURRENT
    if current_file.is_file():
        try:
            current_data = _read_json(current_file)
            if isinstance(current_data.get("score"), (int, float)):
                score = max(score, float(current_data["score"]))
        except PipelineError:
            pass
    return score


def gate(config: LearningConfig, run: Run) -> float:
    if not run.metrics.is_file():
        raise GateError("还没有评测结果：{}".format(run.metrics))
    data = _read_json(run.metrics)
    score = data.get("score")
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        raise GateError("评测结果缺少 score")
    score = float(score)
    baseline = _baseline(config)
    threshold = baseline + config.min_delta
    if score < threshold:
        raise GateError(
            "评测未通过：score={:.4f} < baseline={:.4f} + min_delta={:.4f}".format(
                score, baseline, config.min_delta
            )
        )
    return score


def promote(config: LearningConfig, run: Run) -> Dict[str, Any]:
    score = gate(config, run)
    config.adapters_dir.mkdir(parents=True, exist_ok=True)
    manifest = _read_json(run.manifest) if run.manifest.is_file() else {}
    payload = {
        "run": run.id,
        "adapter": str(run.adapter),
        "score": score,
        "created_at": time.time(),
        "sha256": manifest.get("sha256", ""),
    }
    _write_json(config.adapters_dir / CURRENT, payload)
    with (config.adapters_dir / HISTORY).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    _write_json(run.root / PROMOTE_FILE, payload)
    return payload


def current(config: LearningConfig) -> Optional[Dict[str, Any]]:
    path = config.adapters_dir / CURRENT
    return _read_json(path) if path.is_file() else None


def cycle(
    config: LearningConfig,
    sessions_root: Path,
    *,
    include_failed: bool = False,
    run_id: Optional[str] = None,
) -> Dict[str, Any]:
    run = create_run(config, run_id)
    result: Dict[str, Any] = {"run": run.id, "status": "started"}
    try:
        export_into_run(config, run, sessions_root, include_failed=include_failed)
        train(config, run)
        evaluate(config, run)
        promoted = promote(config, run)
        result = {"run": run.id, "status": "promoted", "promote": promoted}
    except GateError as e:
        result = {"run": run.id, "status": "rejected", "error": str(e)}
    except PipelineError as e:
        result = {"run": run.id, "status": "failed", "error": str(e)}
    except Exception as e:  # noqa: BLE001 - 失败要落盘，不能只留 traceback
        result = {"run": run.id, "status": "failed", "error": str(e)}
    _write_json(run.root / CYCLE_FILE, result)
    return result


def doctor(config: LearningConfig, sessions_root: Path) -> Dict[str, Any]:
    checks: Dict[str, Any] = {
        "root": str(config.root),
        "sessions": str(sessions_root),
        "train": bool(config.train),
        "eval": bool(config.eval),
        "tasks": bool(config.eval_tasks and config.eval_tasks.is_file()),
        "sessions_dir": sessions_root.is_dir(),
    }
    try:
        config.root.mkdir(parents=True, exist_ok=True)
        config.runs_dir.mkdir(parents=True, exist_ok=True)
        config.adapters_dir.mkdir(parents=True, exist_ok=True)
        checks["writable"] = True
    except OSError as e:
        checks["writable"] = False
        checks["error"] = str(e)
    return checks
