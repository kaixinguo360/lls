"""CLI 入口：参数解析、非 tty 透传、doctor / replay 子命令。"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import traceback

__all__ = ["doctor", "main", "parse_args"]


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="lls", description="基于 PTY 的终端包装器")
    p.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="要运行的命令（不填则用 $SHELL）",
    )
    p.add_argument("--doctor", action="store_true", help="诊断环境并退出")
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
    from .tty import supports_posix_terminal

    if supports_posix_terminal():
        print("posix tty: OK (pty/termios/fcntl)")
    else:
        print("posix tty: 缺失 (需要 pty/termios/fcntl；Windows 请用 WSL)")
    print(f"shell:    {_default_shell()}")
    return 0


def _run(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.doctor:
        return doctor()

    command = args.command or [_default_shell()]

    # 非交互：直接执行，不接管终端
    if not sys.stdin.isatty():
        try:
            return subprocess.call(command)
        except FileNotFoundError:
            print(f"lls: command not found: {command[0]}", file=sys.stderr)
            return 127

    # 交互：进入主循环
    from .app import run_interactive

    return run_interactive(command)


def main(argv: list[str] | None = None) -> int:
    """进程最外层。

    任何未捕获异常都打印完整堆栈后以非零码退出 —— 兜底只做这一件事，
    以免遗漏的错误信息被静默吞掉。
    """
    try:
        return _run(argv)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
