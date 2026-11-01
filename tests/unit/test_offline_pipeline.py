"""离线流水线：训练、评测、门禁、晋升和失败隔离。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from lls.ai.sessions import SessionStore
from lls.learn import pipeline
from lls.learn.config import Command, LearningConfig, parse_learning


def _make_session(root: Path) -> None:
    store = SessionStore(root=root)
    session = store.create("stub", "m")
    session.append_turn_start(1)
    session.append_user("列文件", turn=1)
    session.append_assistant("完成", turn=1, step=1)
    session.append_turn_end("ok", turn=1)
    session.close()


def _scripts(tmp_path: Path, score: str = "0.9") -> tuple[Path, Path]:
    trainer = tmp_path / "trainer.py"
    trainer.write_text(
        "import os, pathlib\n"
        "pathlib.Path(os.environ['LLS_ADAPTER'], 'adapter.txt').write_text('ok')\n",
        encoding="utf-8",
    )
    evaluator = tmp_path / "evaluator.py"
    evaluator.write_text(
        "import json, os, pathlib\n"
        "pathlib.Path(os.environ['LLS_METRICS']).write_text("
        "json.dumps({'score': float(os.environ.get('FAKE_SCORE', '0.9')), "
        "'passed': 9, 'total': 10}))\n",
        encoding="utf-8",
    )
    return trainer, evaluator


def _config(tmp_path: Path, score: str = "0.9", *, baseline: float = 0.5) -> LearningConfig:
    trainer, evaluator = _scripts(tmp_path, score)
    tasks = tmp_path / "tasks.jsonl"
    tasks.write_text("{}\n", encoding="utf-8")
    return LearningConfig(
        root=tmp_path / "learning",
        runs_dir=tmp_path / "learning" / "runs",
        data_dir=tmp_path / "learning" / "data",
        adapters_dir=tmp_path / "learning" / "adapters",
        train=Command((sys.executable, str(trainer))),
        eval=Command((sys.executable, str(evaluator))),
        eval_tasks=tasks,
        baseline=baseline,
        min_delta=0.01,
    )


def test_cycle_promotes_only_after_gate(tmp_path: Path, monkeypatch) -> None:
    _make_session(tmp_path / "sessions")
    config = _config(tmp_path)
    monkeypatch.setenv("FAKE_SCORE", "0.9")

    result = pipeline.cycle(config, tmp_path / "sessions", run_id="run-ok")

    assert result["status"] == "promoted"
    assert result["promote"]["score"] == 0.9
    current = pipeline.current(config)
    assert current is not None
    assert current["run"] == "run-ok"
    assert Path(current["adapter"]).is_dir()


def test_cycle_keeps_current_when_gate_rejects(tmp_path: Path, monkeypatch) -> None:
    _make_session(tmp_path / "sessions")
    config = _config(tmp_path, baseline=0.8)
    monkeypatch.setenv("FAKE_SCORE", "0.4")

    result = pipeline.cycle(config, tmp_path / "sessions", run_id="run-bad")

    assert result["status"] == "rejected"
    assert pipeline.current(config) is None


def test_current_adapter_is_the_baseline_for_the_next_cycle(
    tmp_path: Path, monkeypatch
) -> None:
    _make_session(tmp_path / "sessions")
    config = _config(tmp_path, baseline=0.5)
    monkeypatch.setenv("FAKE_SCORE", "0.9")
    first = pipeline.cycle(config, tmp_path / "sessions", run_id="run-first")
    assert first["status"] == "promoted"

    monkeypatch.setenv("FAKE_SCORE", "0.89")
    second = pipeline.cycle(config, tmp_path / "sessions", run_id="run-second")

    assert second["status"] == "rejected"
    assert pipeline.current(config)["run"] == "run-first"


def test_cycle_reports_train_failure_without_publishing(tmp_path: Path) -> None:
    _make_session(tmp_path / "sessions")
    config = _config(tmp_path)
    bad = tmp_path / "bad_trainer.py"
    bad.write_text("raise SystemExit(3)\n", encoding="utf-8")
    config = config.__class__(
        **{**config.__dict__, "train": Command((sys.executable, str(bad)))}
    )

    result = pipeline.cycle(config, tmp_path / "sessions", run_id="run-fail-cycle")

    assert result["status"] == "failed"
    assert pipeline.current(config) is None


def test_eval_rejects_bad_metrics(tmp_path: Path) -> None:
    _make_session(tmp_path / "sessions")
    config = _config(tmp_path)
    bad = tmp_path / "bad_eval.py"
    bad.write_text(
        "import json, os, pathlib\n"
        "pathlib.Path(os.environ['LLS_METRICS']).write_text("
        "'{\"score\": 1.5}')\n",
        encoding="utf-8",
    )
    config = config.__class__(
        **{**config.__dict__, "eval": Command((sys.executable, str(bad)))}
    )
    run = pipeline.create_run(config, "run-bad-eval")
    pipeline.export_into_run(config, run, tmp_path / "sessions")
    pipeline.train(config, run)

    with pytest.raises(pipeline.EvalError):
        pipeline.evaluate(config, run)


def test_train_failure_does_not_publish(tmp_path: Path) -> None:
    _make_session(tmp_path / "sessions")
    config = _config(tmp_path)
    bad = tmp_path / "bad_trainer.py"
    bad.write_text("raise SystemExit(3)\n", encoding="utf-8")
    config = config.__class__(
        **{**config.__dict__, "train": Command((sys.executable, str(bad)))}
    )
    run = pipeline.create_run(config, "run-fail")
    pipeline.export_into_run(config, run, tmp_path / "sessions")

    with pytest.raises(pipeline.PipelineError):
        pipeline.train(config, run)
    assert pipeline.current(config) is None


def test_dataset_hash_is_checked_before_training(tmp_path: Path) -> None:
    _make_session(tmp_path / "sessions")
    config = _config(tmp_path)
    run = pipeline.create_run(config, "run-hash")
    pipeline.export_into_run(config, run, tmp_path / "sessions")
    run.samples.write_text("{}\n", encoding="utf-8")

    with pytest.raises(pipeline.PipelineError):
        pipeline.train(config, run)


def test_parse_learning_paths_and_commands(tmp_path: Path) -> None:
    notes: list[str] = []
    config = parse_learning(
        {
            "root": "learning",
            "train": {"command": ["python", "-m", "trainer"], "timeout": 10},
            "eval": {"command": "python -m evaluator", "tasks": "tasks.jsonl"},
            "baseline": 0.4,
            "min_delta": 0.02,
        },
        base_dir=tmp_path,
        default_root=tmp_path / "default",
        notes=notes,
    )
    assert config.root == tmp_path / "learning"
    assert config.runs_dir == tmp_path / "learning" / "runs"
    assert config.train is not None and config.train.argv == ("python", "-m", "trainer")
    assert config.eval is not None and config.eval.argv == ("python", "-m", "evaluator")
    assert config.eval_tasks == tmp_path / "tasks.jsonl"
    assert notes == []


def test_cli_cycle_runs_end_to_end(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    _make_session(sessions)
    trainer, evaluator = _scripts(tmp_path)
    tasks = tmp_path / "tasks.jsonl"
    tasks.write_text("{}\n", encoding="utf-8")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "paths": {"root": str(tmp_path / "state")},
                "learning": {
                    "root": str(tmp_path / "learning"),
                    "train": {"command": [sys.executable, str(trainer)]},
                    "eval": {
                        "command": [sys.executable, str(evaluator)],
                        "tasks": str(tasks),
                    },
                    "baseline": 0.5,
                    "min_delta": 0.01,
                },
            }
        ),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path.cwd() / "src")
    env["FAKE_SCORE"] = "0.9"

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "lls.learn.cli",
            "--config",
            str(config_path),
            "--sessions",
            str(sessions),
            "cycle",
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(Path.cwd()),
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    pointer = tmp_path / "learning" / "adapters" / "current.json"
    assert json.loads(pointer.read_text("utf-8"))["score"] == 0.9
