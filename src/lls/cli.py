"""CLI 入口：参数解析、非 tty 透传、环境诊断。"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import traceback

__all__ = ["doctor", "main", "parse_args"]


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="lls", description="基于 PTY 的终端包装器")
    p.add_argument("--config", metavar="PATH", help="配置文件路径（默认按 XDG 查找）")
    p.add_argument(
        "--set",
        metavar="KEY=VALUE",
        action="append",
        default=[],
        help="临时覆盖一个配置项，可重复，如 --set input_history.max_entries=50",
    )
    p.add_argument("--doctor", action="store_true", help="诊断环境并退出")
    p.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="要运行的命令（不填则用 $SHELL）",
    )
    return p.parse_args(argv)


def _default_shell() -> str:
    shell = os.environ.get("SHELL")
    if shell and os.path.realpath(shell) != os.path.realpath(sys.argv[0]):
        return shell
    return os.environ.get("LLS_FALLBACK_SHELL", "/bin/sh")


def doctor() -> int:
    import platform

    from .config import load
    from .tty import supports_posix_terminal

    config = load(emit_warnings=False)
    print(f"platform: {platform.platform()}")
    print(f"python:   {sys.version.split()[0]}")
    try:
        import pyte  # noqa: F401

        print("pyte:     OK")
    except ImportError:
        print("pyte:     缺失 (pip install pyte)")
    if supports_posix_terminal():
        print("posix tty: OK (pty/termios/fcntl)")
    else:
        print("posix tty: 缺失 (需要 pty/termios/fcntl；Windows 请用 WSL)")
    print(f"shell:    {_default_shell()}")
    suffix = "" if config.exists else " (missing, using defaults)"
    print(f"config:   {config.path}{suffix}")
    print(f"paths.root:         {config.paths.root}")
    print(f"paths.sessions_dir: {config.paths.sessions_dir}")
    history_dir = config.paths.history_dir
    if not config.input_history.enabled:
        history_dir = f"{history_dir} (disabled)"
    print(f"paths.history_dir:  {history_dir}")
    hist = config.input_history
    print(
        f"input_history:      enabled={str(hist.enabled).lower()} "
        f"max_entries={hist.max_entries} max_bytes={hist.max_bytes}"
    )
    if config.ai_error:
        first_line = config.ai_error.splitlines()[0]
        print(f"ai:       {first_line}")
    for note in config.warnings:
        print(f"warning:  {note}")
    return 0


def _run(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    from .config import ConfigError, configure

    try:
        configure(path=args.config, overrides=args.set)
    except ConfigError as e:
        print(f"lls: {e}", file=sys.stderr)
        return 2

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
