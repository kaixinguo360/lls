"""终端这一族工具：读屏、等待、往用户的终端里写。

它们不认识引擎，也不认识 ``Services``：只要求一个 :class:`TerminalPort` ——
能取屏幕文本、能写字节、能把模型追平、还能「等一会儿并处理键盘」。引擎的
``Services`` 结构上就满足它，于是这里既不用 import 引擎，也不用为测试造一个
假引擎；想把这组工具搬到别的宿主上（另一个 TUI 框架、一个纯 stdin/stdout 的
脚本），照着这三个方法实现一遍就行。

读写分开是有意的：动作工具（``send_*``）只写，读屏只有 ``read_terminal`` 与
``wait_for`` 两个入口 —— 什么时候观察由模型决定，而不是每输入一个按键就回一屏。
"""

from __future__ import annotations

import base64
import binascii
import re
import time
from typing import Any, Awaitable, Callable, Protocol

from ...tty import encode_keys
from ..agent import AgentTool
from ..types import Tool
from .args import as_float, as_int

__all__ = [
    "DEFAULT_READ_LINES",
    "MAX_LINES",
    "READ_TOOL",
    "SEND_KEYS_TOOL",
    "SEND_RAW_TOOL",
    "SEND_TEXT_TOOL",
    "ScreenView",
    "TerminalPort",
    "WAIT_FOR_TOOL",
    "terminal_tools",
]

DEFAULT_READ_LINES = 120  # read_terminal 未给 lines 时的取值
MAX_LINES = 2000
READ_POLL = 0.1           # 读终端时轮询键盘的间隔
READ_QUIET = 0.25         # 画面多久不变算静默
MAX_WAIT = 60.0           # read_terminal 的 wait_seconds 上限
MAX_WAIT_FOR = 600.0      # wait_for 的 timeout 上限：长编译也该等得到


class TerminalPort(Protocol):
    """工具只看得到这几样东西。

    ``screen`` 是子进程的屏幕模型（``text(limit=...)`` 取尾部若干行）；
    ``write`` 往子进程写字节；``flush_model`` 把还没解析的输出喂给模型 ——
    读之前必须先追上，否则读到的是上一刻的画面。
    """

    @property
    def screen(self) -> Any: ...

    def write(self, data: bytes, *, enter: bool = False) -> None: ...

    def flush_model(self) -> None: ...

    async def wait(self, timeout: float) -> None: ...


#: 等的时候「同时处理键盘事件」的回调（Ctrl-C 由它变成取消）。
Poll = Callable[[float], Awaitable[None]]


def _new_lines(before: list[str], after: list[str]) -> list[str]:
    """``after`` 里相对 ``before`` 多出来的行：按出现次数扣，不看位置。

    用行数而不是位置，滚屏和原地重画都不影响判断；同一个提示符第二次出现
    时次数+1，仍然算新行。
    """
    remaining: dict[str, int] = {}
    for line in before:
        remaining[line] = remaining.get(line, 0) + 1
    fresh: list[str] = []
    for line in after:
        count = remaining.get(line, 0)
        if count:
            remaining[line] = count - 1
        else:
            fresh.append(line)
    return fresh


class ScreenView:
    """屏幕快照 + 有限的等待。

    等的时候不自己读键盘，交给宿主的 ``poll``（见 :data:`Poll`）：执行中键盘
    归输入行（这里只等一个中断信号），空闲时那一个仍旧等键盘。

    这里记住「上一次读到的画面」用于省略重复内容 —— 这份记忆也用在任务开头
    的快照上（:meth:`mark`），所以同一份 ``ScreenView`` 要给整条链路共用。
    """

    def __init__(self, port: TerminalPort, poll: Poll) -> None:
        self._port = port
        self._poll = poll
        self._last = ""

    def snapshot(self, lines: int) -> str:
        self._port.flush_model()
        limit = max(1, min(int(lines), MAX_LINES))
        return self._port.screen.text(limit=limit)

    def mark(self, text: str) -> None:
        """记下「模型已经看过」的画面，用于省略重复内容。"""
        self._last = text

    async def read(self, lines: int, wait_seconds: float) -> tuple[str, str]:
        """读屏幕；``wait_seconds`` 内静默就提前返回，否则到点返回。"""
        text = self.snapshot(lines)
        if wait_seconds <= 0:
            return text, "quiet"

        initial = text
        deadline = time.monotonic() + min(max(wait_seconds, 0.0), MAX_WAIT)
        last_change = time.monotonic()
        while True:
            now = time.monotonic()
            await self._poll(min(READ_POLL, max(0.0, deadline - now)))
            fresh = self.snapshot(lines)
            now = time.monotonic()
            if fresh != text:
                text = fresh
                last_change = now
            if now - last_change >= READ_QUIET:
                return text, "quiet"
            if now >= deadline:
                if text == initial:
                    # 等了整段预算却什么都没变：就是安静的。
                    return text, "quiet"
                return text, "still-producing"

    async def wait_for(
        self, lines: int, pattern: str, timeout: float
    ) -> tuple[str, str]:
        """等到新行匹配 ``pattern``（空串：等画面安静）；返回 (画面, 状态)。

        ``pattern`` 只在新出现的行里找。调用时画面上就有的那段不算 —— 等
        「命令跑完、提示符回来」时，上一个提示符本来就在屏幕里。
        """
        limit = max(1, min(int(lines), MAX_LINES))
        self._port.flush_model()
        text = self._port.screen.text(limit=limit)
        before = text.split("\n")
        regex = re.compile(pattern) if pattern else None
        deadline = time.monotonic() + min(max(timeout, 0.0), MAX_WAIT_FOR)
        last_change = time.monotonic()
        while True:
            now = time.monotonic()
            if regex is None and now - last_change >= READ_QUIET:
                return text, "quiet"
            if now >= deadline:
                return text, "timeout"
            await self._poll(min(READ_POLL, max(0.0, deadline - now)))
            fresh = self.snapshot(limit)
            now = time.monotonic()
            if fresh != text:
                text = fresh
                last_change = now
            if regex is not None and regex.search(
                "\n".join(_new_lines(before, text.split("\n")))
            ):
                return text, "matched"

    def observe(self, text: str, status: str) -> str:
        """把一次读取包成观察块；与上次相同则省略正文。"""
        if text == self._last:
            body = "（与上次读取相同）"
        else:
            body = text
            self._last = text
        if not body.strip():
            body = "（屏幕为空）"
        return (
            "<terminal_state>\n"
            + body
            + "\n</terminal_state>\n"
            + "<status>"
            + status
            + "</status>"
        )


# ---- 工具协议 ----

READ_TOOL = Tool(
    name="read_terminal",
    description=(
        "Read the most recent terminal lines. Waits up to wait_seconds for the "
        "screen to stop changing, then returns the tail plus a status."
    ),
    parameters={
        "type": "object",
        "properties": {
            "lines": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_LINES,
                "description": "Trailing lines to return; 120 is a good default.",
            },
            "wait_seconds": {
                "type": "number",
                "minimum": 0,
                "maximum": MAX_WAIT,
                "description": (
                    "Wait up to this many seconds for the screen to stop changing; "
                    "0 reads immediately."
                ),
            },
        },
        "required": ["lines", "wait_seconds"],
        "additionalProperties": False,
    },
    strict=True,
)

SEND_TEXT_TOOL = Tool(
    name="send_text",
    description=(
        "Send literal text (UTF-8, may contain newlines). "
        "enter=true appends a newline, submitting it as a command line."
    ),
    parameters={
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "Characters to write verbatim.",
            },
            "enter": {
                "type": "boolean",
                "description": "Append a newline, like pressing Enter to run a command.",
            },
        },
        "required": ["text", "enter"],
        "additionalProperties": False,
    },
    strict=True,
)

SEND_KEYS_TOOL = Tool(
    name="send_keys",
    description=(
        "Send named keys in order, e.g. [\"ctrl-c\"] or [\"up\", \"up\", \"enter\"]. "
        "Supported: enter, tab, shift-tab, backspace, esc, space, up, down, left, "
        "right, home, end, insert, delete, pageup, pagedown, f1..f12, ctrl-a..z, "
        "alt-<char>. Use send_raw for anything else."
    ),
    parameters={
        "type": "object",
        "properties": {
            "keys": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "description": (
                    "Key names; each is translated to its terminal byte sequence."
                ),
            }
        },
        "required": ["keys"],
        "additionalProperties": False,
    },
    strict=True,
)

SEND_RAW_TOOL = Tool(
    name="send_raw",
    description=(
        "Send raw bytes decoded from base64 or hex. Use it for sequences that "
        "send_keys does not name (for example application-cursor ESC O A) or "
        "non-UTF-8 bytes."
    ),
    parameters={
        "type": "object",
        "properties": {
            "data": {"type": "string", "description": "Encoded bytes."},
            "encoding": {
                "type": "string",
                "enum": ["base64", "hex"],
                "description": "How data is encoded.",
            },
        },
        "required": ["data", "encoding"],
        "additionalProperties": False,
    },
    strict=True,
)


WAIT_FOR_TOOL = Tool(
    name="wait_for",
    description=(
        "Wait, then look: returns as soon as a new line matches the regex "
        "pattern, or (with an empty pattern) once the screen has gone quiet. "
        "Use it instead of calling read_terminal in a loop while a slow command "
        "runs. Returns the tail of the screen plus a status: matched / quiet / "
        "timeout."
    ),
    parameters={
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": (
                    "Python regular expression searched in the lines that appear "
                    "after the call starts (whole screens, not incremental: a "
                    "match already on screen when you call does not count). "
                    "Empty string waits for the screen to stop changing."
                ),
            },
            "timeout": {
                "type": "number",
                "minimum": 0,
                "maximum": MAX_WAIT_FOR,
                "description": "Give up after this many seconds and report timeout.",
            },
            "lines": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_LINES,
                "description": "Trailing lines to return; 120 is a good default.",
            },
        },
        "required": ["pattern", "timeout", "lines"],
        "additionalProperties": False,
    },
    strict=True,
)


# ---- 工具实现 ----

def terminal_tools(port: TerminalPort, view: ScreenView) -> list[AgentTool]:
    """读屏/等待 + 三种写法。``view`` 与宿主共用，好让「上次读到什么」一致。"""

    async def read_terminal(args: dict[str, Any]) -> str:
        lines = as_int(args.get("lines"), DEFAULT_READ_LINES)
        wait = as_float(args.get("wait_seconds"), 0.0)
        text, status = await view.read(lines, wait)
        return view.observe(text, status)

    async def send_text(args: dict[str, Any]) -> str:
        text = args.get("text")
        if not isinstance(text, str):
            raise ValueError("'text' must be a string")
        enter = args.get("enter", True)
        if not isinstance(enter, bool):
            raise ValueError("'enter' must be a boolean")

        data = text.encode("utf-8")
        port.write(data, enter=enter)
        appended = bool(enter and not data.endswith(b"\n"))
        return (
            f"已写入 {len(data) + (1 if appended else 0)} 字节"
            f"{'（末尾已回车）' if appended else ''}。用 read_terminal 查看结果。"
        )

    async def send_keys(args: dict[str, Any]) -> str:
        names = args.get("keys")
        if not isinstance(names, list) or not names:
            raise ValueError("'keys' must be a non-empty array of key names")

        cleaned = [str(name) for name in names]
        data = encode_keys(cleaned)
        port.write(data)
        return f"已发送 {len(cleaned)} 个按键（{len(data)} 字节）：{' '.join(cleaned)}"

    async def send_raw(args: dict[str, Any]) -> str:
        encoded = args.get("data")
        encoding = args.get("encoding")
        if not isinstance(encoded, str) or not encoded:
            raise ValueError("'data' must be a non-empty string")
        if encoding not in ("base64", "hex"):
            raise ValueError("'encoding' must be 'base64' or 'hex'")

        try:
            if encoding == "base64":
                padded = encoded + "=" * (-len(encoded) % 4)
                data = base64.b64decode(padded, validate=True)
            else:
                data = bytes.fromhex(encoded)
        except (binascii.Error, ValueError) as e:
            raise ValueError(f"invalid {encoding} data: {e}") from e
        if not data:
            raise ValueError("decoded data is empty")

        port.write(data)
        return f"已发送 {len(data)} 字节（{encoding}）"

    async def wait_for(args: dict[str, Any]) -> str:
        pattern = args.get("pattern", "")
        if not isinstance(pattern, str):
            raise ValueError("'pattern' must be a string")
        if pattern:
            try:
                re.compile(pattern)
            except re.error as e:
                raise ValueError(f"invalid 'pattern': {e}") from e
        timeout = as_float(args.get("timeout"), MAX_WAIT_FOR)
        lines = as_int(args.get("lines"), DEFAULT_READ_LINES)
        text, status = await view.wait_for(lines, pattern, timeout)
        return view.observe(text, status)

    return [
        AgentTool(READ_TOOL, read_terminal),
        AgentTool(WAIT_FOR_TOOL, wait_for),
        AgentTool(SEND_TEXT_TOOL, send_text),
        AgentTool(SEND_KEYS_TOOL, send_keys),
        AgentTool(SEND_RAW_TOOL, send_raw),
    ]
