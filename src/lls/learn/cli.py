"""``lls-learn``：只读离线入口。

当前提供 ``export``（v2 会话 → SFT JSONL）与 ``doctor``（目录/数据自检）；
训练与评测由仓库外的独立训练器负责，不在主仓库里引入 torch。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .dataset import build_samples, export

__all__ = ["main"]


def _sessions_dir(value: str | None) -> Path:
    if value:
        return Path(value).expanduser()
    from lls.config import load

    return load(emit_warnings=False).paths.sessions_dir


def _doctor(sessions: Path) -> int:
    from lls.ai.sessions import SessionStore

    store = SessionStore(root=sessions)
    metas = store.list()
    samples = sum(1 for _ in build_samples(sessions))
    print("sessions: {}".format(sessions))
    print("stored:   {}".format(len(metas)))
    print("sft:      {} sample(s)".format(samples))
    return 0


def _export(sessions: Path, out: Path, include_failed: bool) -> int:
    manifest = export(sessions, out, include_failed=include_failed)
    print("wrote:    {}".format(out / "sft.jsonl"))
    print("samples:  {}".format(manifest["samples"]))
    print("sessions: {}".format(manifest["sessions"]))
    print("sha256:   {}".format(manifest["sha256"]))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="lls-learn", description="LLS 离线学习：只读会话，导出 SFT 数据"
    )
    parser.add_argument("--sessions", metavar="DIR", help="会话根目录")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("doctor", help="检查会话目录与可导出样本")

    export_cmd = sub.add_parser("export", help="导出 SFT JSONL")
    export_cmd.add_argument("--out", metavar="DIR", required=True, help="输出目录")
    export_cmd.add_argument(
        "--include-failed", action="store_true", help="连失败的轮次也导出"
    )

    args = parser.parse_args(argv)
    sessions = _sessions_dir(args.sessions)
    if args.command in (None, "doctor"):
        return _doctor(sessions)
    if args.command == "export":
        return _export(sessions, Path(args.out), args.include_failed)
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
