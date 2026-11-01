"""``lls-learn``：学习侧的离线入口。

当前只提供自检；训练与导出等离线流程等数据契约稳定后再加。
"""

from __future__ import annotations

import argparse
import sys

__all__ = ["main"]


def _doctor() -> int:
    from lls.config import load

    config = load(emit_warnings=False)
    learning = config.learning
    print("config:    {}".format(config.path))
    print("help:      {}".format("on" if learning.help.enabled else "off"))
    print("teacher:   {}".format(learning.help.teacher_model or "-"))
    print(
        "limits:    per_turn={} per_session={} min_tool_calls={}".format(
            learning.help.max_per_turn,
            learning.help.max_per_session,
            learning.help.min_tool_calls,
        )
    )
    if config.ai_error:
        print("ai:        {}".format(config.ai_error.splitlines()[0]))
    for note in config.warnings:
        print("warning:   {}".format(note))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="lls-learn", description="LLS 学习侧离线工具"
    )
    parser.add_argument("--config", metavar="PATH", help="配置文件路径")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("doctor", help="检查 learning 配置")
    args = parser.parse_args(argv)
    if args.config:
        from lls.config import configure

        configure(path=args.config)
    if args.command in (None, "doctor"):
        return _doctor()
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
