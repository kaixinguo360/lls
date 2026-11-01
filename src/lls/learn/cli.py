"""``lls-learn``：离线数据、训练、评测与晋升入口。

主仓库只负责编排；训练器/评测器是外部命令，通过占位符和 ``LLS_*`` 环境
变量拿路径。没有配置训练命令时，``export`` 仍可单独使用。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import pipeline
from .config import LearningConfig
from .dataset import build_samples, export

__all__ = ["main"]


def _config(path: str | None) -> LearningConfig:
    from lls.config import configure, load

    if path:
        configure(path=path)
    return load(emit_warnings=False).learning


def _sessions_dir(value: str | None) -> Path:
    if value:
        return Path(value).expanduser()
    from lls.config import load

    return load(emit_warnings=False).paths.sessions_dir


def _run(value: str) -> pipeline.Run:
    return pipeline.Run(Path(value).expanduser())


def _doctor(sessions: Path, config: LearningConfig) -> int:
    from lls.ai.sessions import SessionStore

    store = SessionStore(root=sessions)
    metas = store.list()
    samples = sum(1 for _ in build_samples(sessions))
    checks = pipeline.doctor(config, sessions)
    print("sessions: {}".format(sessions))
    print("stored:   {}".format(len(metas)))
    print("sft:      {} sample(s)".format(samples))
    print("root:     {}".format(checks["root"]))
    print("writable: {}".format("yes" if checks.get("writable") else "no"))
    print("train:    {}".format("configured" if config.train else "missing"))
    print("eval:     {}".format("configured" if config.eval else "missing"))
    print("tasks:    {}".format("ok" if checks.get("tasks") else "missing"))
    return 0


def _export(sessions: Path, out: Path, include_failed: bool) -> int:
    manifest = export(sessions, out, include_failed=include_failed)
    print("wrote:    {}".format(out / "sft.jsonl"))
    print("samples:  {}".format(manifest["samples"]))
    print("sessions: {}".format(manifest["sessions"]))
    print("sha256:   {}".format(manifest["sha256"]))
    return 0


def _train(run: pipeline.Run, config: LearningConfig) -> int:
    payload = pipeline.train(config, run)
    print("run:      {}".format(run.id))
    print("adapter:  {}".format(payload["adapter"]))
    print("seconds:  {:.2f}".format(payload["duration"]))
    return 0


def _eval(run: pipeline.Run, config: LearningConfig) -> int:
    payload = pipeline.evaluate(config, run)
    print("run:      {}".format(run.id))
    print("score:    {:.4f}".format(payload["score"]))
    print("passed:   {}".format(payload.get("passed", "-")))
    print("total:    {}".format(payload.get("total", "-")))
    return 0


def _promote(run: pipeline.Run, config: LearningConfig) -> int:
    payload = pipeline.promote(config, run)
    print("promoted: {}".format(payload["adapter"]))
    print("score:    {:.4f}".format(payload["score"]))
    return 0


def _cycle(sessions: Path, config: LearningConfig, include_failed: bool) -> int:
    result = pipeline.cycle(config, sessions, include_failed=include_failed)
    print("run:      {}".format(result["run"]))
    print("status:   {}".format(result["status"]))
    if result["status"] == "promoted":
        print("score:    {:.4f}".format(result["promote"]["score"]))
        return 0
    print("error:    {}".format(result.get("error", "")))
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="lls-learn", description="LLS 离线学习：导出、训练、评测、晋升"
    )
    parser.add_argument("--config", metavar="PATH", help="配置文件路径")
    parser.add_argument("--sessions", metavar="DIR", help="会话根目录")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("doctor", help="检查配置、目录与可导出样本")

    export_cmd = sub.add_parser("export", help="导出 SFT JSONL")
    export_cmd.add_argument("--out", metavar="DIR", required=True, help="输出目录")
    export_cmd.add_argument(
        "--include-failed", action="store_true", help="连失败的轮次也导出"
    )

    for name, help_text in (
        ("train", "对已有运行目录执行训练"),
        ("eval", "对已有运行目录执行评测"),
        ("promote", "评测通过后晋升 adapter"),
    ):
        sub.add_parser(name, help=help_text).add_argument(
            "--run", metavar="DIR", required=True, help="运行目录"
        )

    cycle_cmd = sub.add_parser("cycle", help="导出 → 训练 → 评测 → 晋升")
    cycle_cmd.add_argument(
        "--include-failed", action="store_true", help="连失败的轮次也导出"
    )

    args = parser.parse_args(argv)
    config = _config(args.config)
    sessions = _sessions_dir(args.sessions)
    if args.command in (None, "doctor"):
        return _doctor(sessions, config)
    try:
        if args.command == "export":
            return _export(sessions, Path(args.out), args.include_failed)
        if args.command == "train":
            return _train(_run(args.run), config)
        if args.command == "eval":
            return _eval(_run(args.run), config)
        if args.command == "promote":
            return _promote(_run(args.run), config)
        if args.command == "cycle":
            return _cycle(sessions, config, args.include_failed)
    except pipeline.PipelineError as e:
        print("error: {}".format(e))
        return 1
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
