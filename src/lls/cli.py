"""CLI 入口：参数解析、非 tty 透传、doctor / replay 子命令。"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

__all__ = ["main", "parse_args", "doctor"]


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="lls", description="AI 增强的 shell 终端包装器")
    p.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="要运行的命令（不填则用 $SHELL）",
    )
    p.add_argument("--doctor", action="store_true", help="诊断环境并退出")
    p.add_argument("--config", help="配置文件路径")
    p.add_argument("--role", help="默认 role")
    return p.parse_args(argv)


def _default_shell() -> str:
    shell = os.environ.get("SHELL")
    if shell and os.path.realpath(shell) != os.path.realpath(sys.argv[0]):
        return shell
    return os.environ.get("LLS_FALLBACK_SHELL", "/bin/sh")


def doctor() -> int:
    import platform

    print(f"platform: {platform.platform()}")
    print(f"python:   {sys.version.split()[0]}")
    try:
        import pyte  # noqa: F401

        print("pyte:     OK")
    except ImportError:
        print("pyte:     缺失 (pip install pyte)")
    try:
        import openai  # noqa: F401

        print("openai:   OK")
    except ImportError:
        print("openai:   缺失 (pip install 'lls[openai]')")
    key = os.environ.get("LLS_OPENAI_API_KEY")
    print(f"api_key:  {'已设置' if key else '未设置 (export LLS_OPENAI_API_KEY=...)'}")
    print(f"shell:    {_default_shell()}")
    if sys.platform not in ("linux", "darwin"):
        print("警告: 当前平台无 POSIX 终端，请使用 WSL / Linux / macOS")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.doctor:
        return doctor()

    command = args.command or [_default_shell()]

    # 非交互：透传执行（不过 AI）
    if not sys.stdin.isatty():
        try:
            return subprocess.call(command)
        except FileNotFoundError:
            print(f"lls: command not found: {command[0]}", file=sys.stderr)
            return 127

    # 交互：进入主循环
    from .app import run_interactive

    return run_interactive(command, config=args.config, role=args.role)


if __name__ == "__main__":
    sys.exit(main())
