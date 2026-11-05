"""CLI 入口：全局选项、一级子命令分派、非 tty 透传与环境诊断。"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import traceback

from .subcommands import Command

__all__ = ["doctor", "main", "parse_args"]


def _recording(argv: list[str]) -> int:
    from .config import load
    from .recording.cli import main as recording_main
    from .replay_adapter import row_renderer, screen_factory

    return recording_main(
        argv,
        root=load().paths.recordings_dir,
        screen_factory=screen_factory,
        row_renderer=row_renderer,
    )


COMMANDS: dict[str, Command] = {
    "recording": Command("recording", "列出、回放与导出终端录制", _recording),
}


def _command_help() -> str:
    return "\n".join(
        f"  {command.name:<12}{command.summary}"
        for command in COMMANDS.values()
    )


def _default_shell() -> str:
    shell = os.environ.get("SHELL")
    if shell and os.path.realpath(shell) != os.path.realpath(sys.argv[0]):
        return shell
    return os.environ.get("LLS_FALLBACK_SHELL", "/bin/sh")


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="lls",
        description="基于 PTY 的终端包装器",
        epilog=f"子命令：\n{_command_help()}\n\n其余参数作为要运行的命令。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
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
        print("pyte:     缺失（pip install pyte）")
    if supports_posix_terminal():
        print("posix tty: OK（pty/termios/fcntl）")
    else:
        print("posix tty: 缺失（需要 pty/termios/fcntl；Windows 请使用 WSL）")
    print(f"shell:    {_default_shell()}")
    suffix = "" if config.exists else "（缺失，使用默认值）"
    print(f"config:   {config.path}{suffix}")
    print(f"paths.root:         {config.paths.root}")
    print(f"paths.sessions_dir: {config.paths.sessions_dir}")
    print(f"paths.recordings_dir: {config.paths.recordings_dir}")
    history_dir = config.paths.history_dir
    if not config.input_history.enabled:
        history_dir = f"{history_dir}（已禁用）"
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
            print(f"lls: 未找到命令：{command[0]}", file=sys.stderr)
            return 127

    # 交互：进入主循环
    from .app import run_interactive

    return run_interactive(command)


def _split_global(argv: list[str]) -> tuple[list[str], list[str], bool]:
    """把全局选项与命令分开。

    全局选项只在第一个位置参数之前生效；``--`` 之后一律当命令，用来运行与
    子命令同名的程序。``lls bash --config x`` 里的 ``--config`` 不属于全局。
    """
    head: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--":
            return head, argv[index + 1 :], True
        if token in ("--config", "--set"):
            head.extend(argv[index : index + 2])
            index += 2
            continue
        if token.startswith("--config=") or token.startswith("--set="):
            head.append(token)
            index += 1
            continue
        if token in ("--doctor", "-h", "--help"):
            head.append(token)
            index += 1
            continue
        break
    return head, argv[index:], False


def _global_options(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--config")
    parser.add_argument("--set", action="append", default=[])
    parser.add_argument("--doctor", action="store_true")
    return parser.parse_args(argv)


def _main(argv: list[str] | None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    head, rest, escaped = _split_global(argv)
    if "-h" in head or "--help" in head:
        return _run(["-h"] if "-h" in head else ["--help"])
    if not escaped and rest and rest[0] in COMMANDS:
        from .config import ConfigError, configure

        options = _global_options(head)
        try:
            configure(path=options.config, overrides=options.set)
        except ConfigError as e:
            print(f"lls: {e}", file=sys.stderr)
            return 2
        if options.doctor:
            return doctor()
        return COMMANDS[rest[0]].run(rest[1:])
    return _run(head + rest)


def main(argv: list[str] | None = None) -> int:
    """进程最外层。

    任何未捕获异常都打印完整堆栈后以非零码退出 —— 兜底只做这一件事，
    以免遗漏的错误信息被静默忽略。
    """
    try:
        return _main(argv)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
