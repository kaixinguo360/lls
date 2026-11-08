"""终端这一族工具：读屏、等待、往用户的终端里写。

它们不认识引擎，也不认识 ``Services``：只要求一个 :class:`TerminalPort` ——
能取屏幕文本、能写字节、能把模型追平、还能「等一会儿并处理键盘」。引擎的
``Services`` 结构上就满足它，于是这里既不用 import 引擎，也不用为测试造一个
假引擎；想把这组工具搬到别的宿主上（另一个 TUI 框架、一个纯 stdin/stdout 的
脚本），照着这三个方法实现一遍就行。

读写分开是有意的：动作工具（``send_*``）只写，读屏只有 ``read_terminal``、
``wait_for`` 与 ``wait_for_change`` 三个入口 —— 什么时候观察由模型决定，而
不是每输入一个按键就回一屏。
"""

from __future__ import annotations

import base64
import binascii
import copy
import re
import time
from dataclasses import dataclass, replace
from typing import Any, Awaitable, Callable, Mapping, Protocol

from ...tty import encode_keys
from ..agent import AgentTool
from ..types import Tool
from .args import as_float, as_int
from .observation import Observation

__all__ = [
    "DEFAULT_READ_LINES",
    "MAX_LINES",
    "READ_TOOL",
    "ReadTerminalSettings",
    "RESIZE_TOOL",
    "SEND_KEYS_TOOL",
    "SEND_RAW_TOOL",
    "SEND_TEXT_TOOL",
    "ScreenView",
    "TerminalPort",
    "WAIT_FOR_TOOL",
    "WAIT_FOR_CHANGE_TOOL",
    "terminal_tools",
]

DEFAULT_READ_LINES = 120  # read_terminal 未给 lines 时的取值
MAX_LINES = 2000
READ_POLL = 0.1           # 读终端时轮询键盘的间隔
READ_QUIET = 0.25         # 画面多久不变算静默
MAX_WAIT = 60.0           # read_terminal 的 wait_seconds 上限
MAX_WAIT_FOR = 600.0      # wait_for 的 timeout 上限：长编译也该等得到
MAX_RESIZE_ROWS = 500     # resize_terminal 的行数上限
MAX_RESIZE_COLUMNS = 1000 # resize_terminal 的列数上限

#: 括号粘贴的开关与两个边界。终端只在子进程主动打开 ``?2004`` 时才认它们。
PASTE_MODE = 2004
PASTE_OPEN = b"\x1b[200~"
PASTE_CLOSE = b"\x1b[201~"


@dataclass(frozen=True)
class ReadTerminalSettings:
    """单个读屏工具的特有设置；配置在该工具自己的 ``settings`` 下。

    ``visible_only`` 打开时，这个工具不再暴露 ``lines`` 参数，只返回宿主终端
    **当前可见**的那一屏（不含回滚区）。三个读屏工具各配各的，互不影响。

    任务开头的画面快照与 ``generate`` 的上下文没有自己的开关，跟随
    ``read_terminal`` 的设置。
    """

    visible_only: bool = False

    @classmethod
    def from_options(
        cls, options: Any, *, default: bool = False
    ) -> "ReadTerminalSettings":
        """从 :class:`~lls.ai.tool_options.ToolOptions` 里取设置。

        工具自己没写 ``visible_only`` 时用 ``default``（``ai.screen`` 的全局值）。
        """
        raw = getattr(options, "settings", None)
        if not isinstance(raw, Mapping) or "visible_only" not in raw:
            return cls(visible_only=default)
        return cls(visible_only=raw.get("visible_only") is True)


class TerminalPort(Protocol):
    """工具只看得到这几样东西。

    ``screen`` 是子进程的屏幕模型（``text(limit=...)`` 取尾部若干行）；
    ``write`` 往子进程写字节；``flush_model`` 把还没解析的输出喂给模型 ——
    读之前必须先追上，否则读到的是上一刻的画面；``resize`` 把模型与子进程
    一起调到给定尺寸，返回实际生效的行列（工具有权要求的、比宿主窗口小）。
    """

    @property
    def screen(self) -> Any: ...

    def write(self, data: bytes, *, enter: bool = False) -> None: ...

    def flush_model(self) -> None: ...

    async def wait(self, timeout: float) -> None: ...

    def resize(self, rows: int, columns: int) -> tuple[int, int]: ...


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

    def __init__(
        self,
        port: TerminalPort,
        poll: Poll,
        *,
        visible_only: bool = False,
    ) -> None:
        self._port = port
        self._poll = poll
        self._last = ""
        # 没有单独参数时的默认值：任务开头的快照用它。
        self._visible_only = visible_only

    @property
    def visible_only(self) -> bool:
        return self._visible_only

    def snapshot(self, lines: int, *, visible_only: bool | None = None) -> str:
        if visible_only is None:
            visible_only = self._visible_only
        self._port.flush_model()
        if visible_only:
            # 当前可见的一屏；模型看不到回滚区。
            return self._port.screen.text(
                limit=max(1, int(self._port.screen.rows)),
                include_scrollback=False,
            )
        limit = max(1, min(int(lines), MAX_LINES))
        return self._port.screen.text(limit=limit)

    def mark(self, text: str) -> None:
        """记下「模型已经看过」的画面，用于省略重复内容。"""
        self._last = text

    async def read(
        self, lines: int, wait_seconds: float, *, visible_only: bool = False
    ) -> tuple[str, str]:
        """读屏幕；``wait_seconds`` 内静默就提前返回，否则到点返回。"""
        text = self.snapshot(lines, visible_only=visible_only)
        if wait_seconds <= 0:
            return text, "quiet"

        initial = text
        deadline = time.monotonic() + min(max(wait_seconds, 0.0), MAX_WAIT)
        last_change = time.monotonic()
        while True:
            now = time.monotonic()
            await self._poll(min(READ_POLL, max(0.0, deadline - now)))
            fresh = self.snapshot(lines, visible_only=visible_only)
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
        self,
        lines: int,
        pattern: str,
        timeout: float,
        *,
        visible_only: bool = False,
    ) -> tuple[str, str]:
        """等到新行匹配 ``pattern``（空串：等画面安静）；返回 (画面, 状态)。

        ``pattern`` 只在新出现的行里找。调用时画面上就有的那段不算 —— 等
        「命令跑完、提示符回来」时，上一个提示符本来就在屏幕里。
        """
        limit = max(1, min(int(lines), MAX_LINES))
        text = self.snapshot(limit, visible_only=visible_only)
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
            fresh = self.snapshot(limit, visible_only=visible_only)
            now = time.monotonic()
            if fresh != text:
                text = fresh
                last_change = now
            if regex is not None and regex.search(
                "\n".join(_new_lines(before, text.split("\n")))
            ):
                return text, "matched"

    async def wait_for_change(
        self, lines: int, timeout: float, *, visible_only: bool = False
    ) -> tuple[str, str]:
        """等到画面**变了**：返回 (新画面, "changed")，超时返回 (旧画面, "timeout")。

        等的是变化，不是安静：画面本来就安静时 :meth:`read` 会立刻返回，
        拿它守着一个终端就是空转。也不要求某个模式匹配 —— 多一行、少一行、
        某一行改了字都算，这是「没有任务、只对发生的事反应」需要的那一种。
        """
        text = self.snapshot(lines, visible_only=visible_only)
        deadline = time.monotonic() + min(max(timeout, 0.0), MAX_WAIT_FOR)
        while True:
            now = time.monotonic()
            if now >= deadline:
                return text, "timeout"
            await self._poll(min(READ_POLL, max(0.0, deadline - now)))
            fresh = self.snapshot(lines, visible_only=visible_only)
            if fresh != text:
                return fresh, "changed"

    def observe(self, text: str, status: str) -> Observation:
        """把一次读取包成观察块；与上次相同则省略正文。

        抬头那四个属性是「我在看什么」：窗口多大、光标在第几行第几列（1-based）、
        当前是主屏还是备用屏。没有它们，模型驱动全屏程序只能靠画面差分猜位置。

        同一份几何也进 ``data``：文本是给模型的，事实是给记录与统计的 ——
        从一句抬头里再把它解析出来容易出错。
        """
        if text == self._last:
            body = "（与上次读取相同）"
        else:
            body = text
            self._last = text
        if not body.strip():
            body = "（屏幕为空）"
        screen = self._port.screen
        cursor = screen.cursor
        buffer = "alternate" if screen.alternate else "main"
        head = '<terminal_state rows="{}" cols="{}" cursor="{},{}" buffer="{}">'.format(
            screen.rows,
            screen.columns,
            cursor.y + 1,
            cursor.x + 1,
            buffer,
        )
        return Observation(
            head
            + "\n"
            + body
            + "\n</terminal_state>\n"
            + "<status>"
            + status
            + "</status>",
            {
                "status": status,
                "rows": screen.rows,
                "cols": screen.columns,
                "cursor": [cursor.y + 1, cursor.x + 1],
                "buffer": buffer,
            },
        )


# ---- 工具协议 ----

READ_TOOL = Tool(
    name="read_terminal",
    description=(
        "Read the most recent terminal lines. Waits up to wait_seconds for the "
        "screen to stop changing, then returns the tail plus a status. The "
        "opening tag carries the geometry: rows/cols are the window size, "
        "cursor is the 1-based 'row,col' caret position, and buffer is 'main' "
        "or 'alternate' (alternate means a full-screen program such as vim or "
        "less owns the screen)."
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
        "Send literal text (UTF-8, may contain newlines). enter=true appends a "
        "newline, submitting it as a command line. Text containing newlines is "
        "sent as a bracketed paste when the child has bracketed paste enabled; "
        "single-line text is always typed key by key."
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

WAIT_FOR_CHANGE_TOOL = Tool(
    name="wait_for_change",
    description=(
        "Wait until anything on screen changes, then look: returns as soon as "
        "the screen differs from what it was when the call started (new output, "
        "a line gone, a line rewritten), or after the timeout. Unlike wait_for "
        "with an empty pattern it does not wait for the screen to go quiet, so "
        "it is the one to sit on while you have nothing to do yet. Returns the "
        "tail of the screen plus a status: changed / timeout."
    ),
    parameters={
        "type": "object",
        "properties": {
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
        "required": ["timeout", "lines"],
        "additionalProperties": False,
    },
    strict=True,
)

_READ_PROMPT_VISIBLE = (
    "- read_terminal(wait_seconds)：读取终端当前画面（整个可见区域，不含回滚）。\n"
    "  wait_seconds 内画面静默就立即返回；超时返回 still-producing。动作工具不会自动\n"
    "  返回画面，需要观察时自己调用它。观察抬头带 rows/cols/cursor/buffer：窗口尺寸、\n"
    "  1-based 光标行列、当前是主屏还是全屏程序的备用屏（buffer=alternate 时先想清楚\n"
    "  怎么退出）。"
)

_WAIT_FOR_PROMPT_VISIBLE = (
    "- wait_for(pattern, timeout)：等画面出现 pattern（Python 正则，只在\n"
    "  新出现的行里找；空串表示等画面安静下来）。返回当前可见画面+状态（matched/\n"
    "  quiet/timeout）。慢命令用它等结果，不要反复 read_terminal 轮询。"
)

_WAIT_FOR_CHANGE_PROMPT_VISIBLE = (
    "- wait_for_change(timeout)：等到画面**变了**就返回，不看它是否安静。\n"
    "  没有事情可做、只是在盯着终端时用它挂着；不要用 read_terminal 反复轮询。"
)

_VISIBLE_READ_DESCRIPTION = (
    "Read the terminal's visible screen (no scrollback). Waits up to "
    "wait_seconds for the screen to stop changing, then returns the screen plus "
    "a status. The opening tag carries the geometry: rows/cols are the window "
    "size, cursor is the 1-based 'row,col' caret position, and buffer is 'main' "
    "or 'alternate' (alternate means a full-screen program such as vim or less "
    "owns the screen)."
)

_VISIBLE_WAIT_FOR_DESCRIPTION = (
    "Wait, then look at the visible screen (no scrollback): returns as soon as a "
    "new line matches the regex pattern, or (with an empty pattern) once the "
    "screen has gone quiet. Returns the visible screen plus a status: matched / "
    "quiet / timeout."
)

_VISIBLE_WAIT_FOR_CHANGE_DESCRIPTION = (
    "Wait until anything on the visible screen changes (no scrollback), then "
    "look: returns as soon as the screen differs from what it was when the call "
    "started, or after the timeout. Returns the visible screen plus a status: "
    "changed / timeout."
)


def _without_lines(parameters: dict[str, Any]) -> dict[str, Any]:
    """复制一份 Schema，去掉模型可见的 ``lines`` 参数。"""
    copied = copy.deepcopy(parameters)
    properties = copied.get("properties")
    if isinstance(properties, dict):
        properties.pop("lines", None)
    required = copied.get("required")
    if isinstance(required, list):
        copied["required"] = [name for name in required if name != "lines"]
    return copied


def _read_tool(settings: ReadTerminalSettings) -> Tool:
    if not settings.visible_only:
        return READ_TOOL
    return replace(
        READ_TOOL,
        description=_VISIBLE_READ_DESCRIPTION,
        parameters=_without_lines(READ_TOOL.parameters),
        prompt=_READ_PROMPT_VISIBLE,
    )


def _wait_for_tool(settings: ReadTerminalSettings) -> Tool:
    if not settings.visible_only:
        return WAIT_FOR_TOOL
    return replace(
        WAIT_FOR_TOOL,
        description=_VISIBLE_WAIT_FOR_DESCRIPTION,
        parameters=_without_lines(WAIT_FOR_TOOL.parameters),
        prompt=_WAIT_FOR_PROMPT_VISIBLE,
    )


def _wait_for_change_tool(settings: ReadTerminalSettings) -> Tool:
    if not settings.visible_only:
        return WAIT_FOR_CHANGE_TOOL
    return replace(
        WAIT_FOR_CHANGE_TOOL,
        description=_VISIBLE_WAIT_FOR_CHANGE_DESCRIPTION,
        parameters=_without_lines(WAIT_FOR_CHANGE_TOOL.parameters),
        prompt=_WAIT_FOR_CHANGE_PROMPT_VISIBLE,
    )


RESIZE_TOOL = Tool(
    name="resize_terminal",
    description=(
        "Resize the window the child sees: the screen model and its pty are both "
        "resized and the child gets SIGWINCH. Only sizes up to the host terminal "
        "are accepted (a smaller window is like a split pane); a real window "
        "resize overrides it."
    ),
    parameters={
        "type": "object",
        "properties": {
            "rows": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_RESIZE_ROWS,
                "description": "New height in lines.",
            },
            "columns": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_RESIZE_COLUMNS,
                "description": "New width in columns.",
            },
        },
        "required": ["rows", "columns"],
        "additionalProperties": False,
    },
    strict=True,
)


# ---- 工具实现 ----

def terminal_tools(
    port: TerminalPort,
    view: ScreenView,
    *,
    settings: Mapping[str, ReadTerminalSettings] | None = None,
) -> list[AgentTool]:
    """读屏/等待 + 三种写法。``view`` 与宿主共用，好让「上次读到什么」一致。

    ``settings`` 按工具名分开：每个读屏工具只看自己那一项，互不影响。
    """
    options = settings or {}
    read_settings = options.get("read_terminal", ReadTerminalSettings())
    wait_for_settings = options.get("wait_for", ReadTerminalSettings())
    wait_for_change_settings = options.get(
        "wait_for_change", ReadTerminalSettings()
    )
    read_tool = _read_tool(read_settings)
    wait_for_tool = _wait_for_tool(wait_for_settings)
    wait_for_change_tool = _wait_for_change_tool(wait_for_change_settings)

    def lines_for(
        args: dict[str, Any], settings: ReadTerminalSettings
    ) -> int:
        if settings.visible_only:
            return max(1, int(getattr(port.screen, "rows", DEFAULT_READ_LINES)))
        return as_int(args.get("lines"), DEFAULT_READ_LINES)

    async def read_terminal(args: dict[str, Any]) -> str:
        lines = lines_for(args, read_settings)
        wait = as_float(args.get("wait_seconds"), 0.0)
        text, status = await view.read(
            lines, wait, visible_only=read_settings.visible_only
        )
        return view.observe(text, status)

    async def send_text(args: dict[str, Any]) -> str:
        text = args.get("text")
        if not isinstance(text, str):
            raise ValueError("'text' must be a string")
        enter = args.get("enter", True)
        if not isinstance(enter, bool):
            raise ValueError("'enter' must be a boolean")

        data = text.encode("utf-8")
        written = len(data)  # 报给模型的字节数不含粘贴两端的协议包裹
        pasted = b"\n" in data and PASTE_MODE in port.screen.host_modes
        if pasted:
            # 多行文本必须走括号粘贴：逐键写进去会被 readline/vim 当成一次次
            # 键入 —— 每行都被执行、自动缩进叠加。单行不包：``send_text("i")``
            # 在 vim 里必须是「进入插入模式」，包成粘贴就变成一个普通字符。
            data = PASTE_OPEN + data + PASTE_CLOSE
        port.write(data, enter=enter)
        appended = bool(enter and not data.endswith(b"\n"))
        return Observation(
            f"已写入 {written + (1 if appended else 0)} 字节"
            f"{'（括号粘贴）' if pasted else ''}"
            f"{'（末尾已回车）' if appended else ''}。用 read_terminal 查看结果。",
            {"bytes": written + (1 if appended else 0), "paste": pasted},
        )

    async def send_keys(args: dict[str, Any]) -> str:
        names = args.get("keys")
        if not isinstance(names, list) or not names:
            raise ValueError("'keys' must be a non-empty array of key names")

        cleaned = [str(name) for name in names]
        data = encode_keys(cleaned)
        port.write(data)
        return Observation(
            f"已发送 {len(cleaned)} 个按键（{len(data)} 字节）：{' '.join(cleaned)}",
            {"bytes": len(data), "keys": cleaned},
        )

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
        return Observation(
            f"已发送 {len(data)} 字节（{encoding}）",
            {"bytes": len(data), "encoding": encoding},
        )

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
        text, status = await view.wait_for(
            lines_for(args, wait_for_settings),
            pattern,
            timeout,
            visible_only=wait_for_settings.visible_only,
        )
        return view.observe(text, status)

    async def wait_for_change(args: dict[str, Any]) -> str:
        timeout = as_float(args.get("timeout"), MAX_WAIT_FOR)
        text, status = await view.wait_for_change(
            lines_for(args, wait_for_change_settings),
            timeout,
            visible_only=wait_for_change_settings.visible_only,
        )
        return view.observe(text, status)

    async def resize_terminal(args: dict[str, Any]) -> str:
        rows = as_int(args.get("rows"), 0)
        columns = as_int(args.get("columns"), 0)
        if rows <= 0 or columns <= 0:
            raise ValueError("'rows' and 'columns' must be positive")

        actual_rows, actual_columns = port.resize(rows, columns)
        clamped = (actual_rows, actual_columns) != (rows, columns)
        return Observation(
            "终端已调整为 {}×{}{}，子进程收到 SIGWINCH。"
            "用 read_terminal 查看重排结果。".format(
                actual_rows,
                actual_columns,
                "（宿主窗口更小，已按它收窄）" if clamped else "",
            ),
            {"rows": actual_rows, "cols": actual_columns, "clamped": clamped},
        )

    return [
        AgentTool(read_tool, read_terminal),
        AgentTool(wait_for_tool, wait_for),
        AgentTool(wait_for_change_tool, wait_for_change),
        AgentTool(SEND_TEXT_TOOL, send_text),
        AgentTool(SEND_KEYS_TOOL, send_keys),
        AgentTool(SEND_RAW_TOOL, send_raw),
        AgentTool(RESIZE_TOOL, resize_terminal),
    ]
