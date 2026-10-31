"""``bash`` 工具：在用户终端之外另开一个 shell 跑批处理。

要的是退出码和完整输出，而不是屏幕上的画面：git、测试、构建、批量看文件。
它看不见用户的终端，也驱动不了 TUI —— 要交互仍然归 ``send_text`` 与
``read_terminal``。

跨调用只保留一样东西：当前目录。``cd`` 之后下一次调用还在那儿，如同人连续输入
命令那样。这份状态属于**宿主**而不是某个会话（同一台机器，同一个位置），所以
默认用一个进程共用的实例，需要隔开时再自己 ``Shell()`` 一个。

输出走临时文件而不是管道：超时杀掉进程组之后，已经写出来的部分仍然拿得回，
也不会因为管道写满而死锁。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import tempfile
import time
from typing import Any

from ..agent import AgentTool
from ..types import Tool
from .args import as_float

__all__ = ["BASH_TOOL", "SHELL_BIN", "Shell", "shared", "shell_tools"]

DEFAULT_BASH_TIMEOUT = 120.0  # bash 未给 timeout 时的取值
MAX_BASH_TIMEOUT = 600.0      # bash 的 timeout 上限
MAX_BASH_CHARS = 12_000       # bash 的 stdout / stderr 各自最多回多少字符
BASH_KEEP_TAIL = 0.75         # 截断时尾部留多少（头部留剩下的）

BASH_TOOL = Tool(
    name="bash",
    description=(
        "Run a shell command in a separate bash process and return its exit "
        "code, stdout and stderr. This is NOT the user's terminal: it starts in "
        "the given cwd (or where the previous bash call ended, so cd sticks), "
        "prints nothing on screen, inherits no interactive state, and cannot "
        "drive TUI programs — use send_text/read_terminal for anything the user "
        "must see or anything interactive. Use bash for batch work: inspecting "
        "files, git, tests, builds — you get the exit code and the whole output."
    ),
    parameters={
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": (
                    "Shell text, run by bash (falling back to sh). Multi-line "
                    "and heredocs are fine."
                ),
            },
            "cwd": {
                "type": "string",
                "description": (
                    "Working directory; ~ is expanded and relative paths resolve "
                    "against the current one. Empty string keeps the current one."
                ),
            },
            "timeout": {
                "type": "number",
                "minimum": 0,
                "maximum": MAX_BASH_TIMEOUT,
                "description": "Seconds before the command is killed.",
            },
        },
        "required": ["command", "cwd", "timeout"],
        "additionalProperties": False,
    },
    strict=True,
)

SHELL_BIN = shutil.which("bash") or shutil.which("sh") or "/bin/sh"


class Shell:
    """一条「另开的 shell」：只有一个当前目录。"""

    def __init__(self, cwd: str | None = None) -> None:
        self.cwd = cwd or os.getcwd()

    def target(self, raw: Any) -> str:
        """把 ``cwd`` 参数解析成绝对路径；空串表示沿用当前目录。"""
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            return self.cwd
        if not isinstance(raw, str):
            raise ValueError("'cwd' must be a string")
        path = os.path.expanduser(raw)
        if not os.path.isabs(path):
            path = os.path.join(self.cwd, path)
        return os.path.normpath(path)

    async def run(
        self, command: str, cwd: str, timeout: float
    ) -> tuple[int, str, str, float, bool]:
        """跑一条命令：返回 (退出码, stdout, stderr, 耗时, 是否超时)。"""
        if not os.path.isdir(cwd):
            return 125, "", "没有这个目录：{}".format(cwd), 0.0, False
        if not os.path.exists(SHELL_BIN):
            return 127, "", "找不到可用的 shell：{}".format(SHELL_BIN), 0.0, False
        work = tempfile.mkdtemp(prefix="lls-bash-")
        try:
            script = os.path.join(work, "cmd.sh")
            pwd_file = os.path.join(work, "pwd")
            out_path = os.path.join(work, "out")
            err_path = os.path.join(work, "err")
            text = command if command.endswith("\n") else command + "\n"
            # 记下命令自己的退出码；pwd 用位置参数传，省得给路径加引号。
            text += 'status=$?\npwd -P > "$1" 2>/dev/null\nexit $status\n'
            with open(script, "w", encoding="utf-8") as handle:
                handle.write(text)
            env = dict(os.environ)
            # 别让分页器、颜色把上下文占满，也别让 git 卡在问账号密码上。
            env.update(
                TERM="dumb",
                PAGER="cat",
                GIT_PAGER="cat",
                GIT_TERMINAL_PROMPT="0",
                LLS_AGENT="1",
            )
            flags: dict[str, Any] = {}
            if os.name == "posix":
                flags["start_new_session"] = True  # 自成一组，超时能整组收掉
            started = time.monotonic()
            with open(out_path, "wb") as out, open(err_path, "wb") as err:
                proc = await asyncio.create_subprocess_exec(
                    SHELL_BIN,
                    script,
                    pwd_file,
                    cwd=cwd,
                    env=env,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=out,
                    stderr=err,
                    **flags,
                )
                waiter = asyncio.ensure_future(proc.wait())
                timed_out = False
                try:
                    code = await asyncio.wait_for(asyncio.shield(waiter), timeout)
                except asyncio.TimeoutError:
                    timed_out = True
                    _kill_group(proc)
                    code = await waiter
            elapsed = time.monotonic() - started
            try:
                with open(pwd_file, encoding="utf-8", errors="replace") as handle:
                    left = handle.read().strip()
            except OSError:
                left = ""
            if left and os.path.isdir(left):
                self.cwd = left
            return code, _capture(out_path), _capture(err_path), elapsed, timed_out
        finally:
            shutil.rmtree(work, ignore_errors=True)


_SHARED = Shell()


def shared() -> Shell:
    """进程共用的那个 shell：会话之间也共用一个当前位置。"""
    return _SHARED


def _clip_output(text: str, budget: int) -> tuple[str, bool]:
    """把命令输出裁到 ``budget`` 字符：头留一小截、尾留一大截，中间省略。

    与显示层的 ``_clip``（按列宽硬截断）不是一回事：这里的「预算」是字符
    数，留不留省略号也由自己决定。
    """
    if len(text) <= budget:
        return text, False
    tail = int(budget * BASH_KEEP_TAIL)
    head = budget - tail
    skipped = len(text) - budget
    cut = "\n…（中间省略 {} 字符）…\n".format(skipped)
    return text[:head] + cut + text[-tail:], True


def _capture(path: str) -> str:
    """读一段捕获下来的输出（临时文件），按字符预算裁一刀。"""
    try:
        with open(path, "rb") as handle:
            data = handle.read(MAX_BASH_CHARS * 4 + 1)
    except OSError:
        return ""
    return _clip_output(data.decode("utf-8", "replace"), MAX_BASH_CHARS)[0]


def _kill_group(proc: Any) -> None:
    """超时收摊：连子进程一起收，别留下还在跑的 make。"""
    try:
        if os.name == "posix":
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        else:  # pragma: no cover - Windows 上没有进程组
            proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            pass


async def bash(args: dict[str, Any], shell: Shell) -> str:
    """观察文本 = 一句抬头（退出码、耗时、当前位置）+ 两段输出。"""
    command = args.get("command")
    if not isinstance(command, str) or not command.strip():
        raise ValueError("'command' must be a non-empty string")
    cwd = shell.target(args.get("cwd"))
    timeout = max(
        0.0,
        min(as_float(args.get("timeout"), DEFAULT_BASH_TIMEOUT), MAX_BASH_TIMEOUT),
    )
    code, out, err, elapsed, timed_out = await shell.run(command, cwd, timeout)
    if timed_out:
        head = "（已中止：超过 {:g}s，进程组已杀掉）退出码 {}，用时 {:.2f}s，cwd {}".format(
            timeout, code, elapsed, shell.cwd
        )
    else:
        head = "（退出码 {}，用时 {:.2f}s，cwd {}）".format(code, elapsed, shell.cwd)
    parts = [head]
    if out.strip():
        parts.append("--- stdout ---\n" + out.rstrip("\n"))
    if err.strip():
        parts.append("--- stderr ---\n" + err.rstrip("\n"))
    if len(parts) == 1:
        parts.append("（没有输出）")
    return "\n".join(parts)


def shell_tools(shell: Shell) -> list[AgentTool]:
    """shell 这一族：只依赖操作系统，与终端、会话无关。"""

    async def run(args: dict[str, Any]) -> str:
        return await bash(args, shell)

    return [AgentTool(BASH_TOOL, run)]
