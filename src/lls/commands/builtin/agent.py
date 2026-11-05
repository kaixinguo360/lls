"""agent —— 交互式 Agent 会话：每输入一个任务，自动多轮执行直到模型给出答复。

一次 ``agent`` 调用就是一个小型 REPL：

1. 用 ``(agent)`` 提示读入任务；
2. 把当前终端画面与任务交给 :class:`lls.ai.Agent`；
3. 模型反复「调用工具 → 看结果」，直到某一轮不再调用工具（文本即最终答复）；
4. 答复写回会话日志，重新提示下一个任务；历史跨轮保留。

一轮任务里与界面无关的部分（快照、裁历史、压缩、Agent 循环、写事件流）
住在 :mod:`lls.ai.turn`；本模块保留交互、显示与会话内命令。

工具本身住在 :mod:`lls.ai.tools`（协议定义 + 实现 + 中间件），本模块只做
装配：装进工具箱、把执行过程显示成一屏。换一个命令、换一个宿主，工具照旧。

读取终端（``read_terminal``）是独立工具，动作工具只写不读 —— 何时观察由模型
自己决定。``send_text`` 覆盖任意 Unicode 文本，``send_keys`` 覆盖具名按键，
``send_raw`` 用 base64/hex 覆盖任意字节。``wait_for`` 把「等」也变成一次调用：
等到画面出现某段文字（或安静下来）再返回，慢命令不必靠反复读屏轮询。

``read_file`` / ``write_file`` 补上屏幕之外的通道：日志、源码这类东西不必先
``cat`` 到终端再回读，也不会因为滚出屏幕而看不到。``bash`` 再补一条「另开
一个 shell」的通道：git、测试、构建这类批处理要的是退出码和完整输出，不必
挤进 PTY；它看不见用户的终端，也驱动不了 TUI，所以交互仍归 ``send_text`` 与
``read_terminal``。``cd`` 会留到下一次调用 —— 那是这个 shell 自己的当前目录。

继续/结束的规则与 DSH / opencode 一致：**没有工具调用就是正常结束**，不设
finish 工具。本命令不设步数上限（长任务靠 Ctrl-C 中止）；通用循环
:class:`lls.ai.Agent` 仍可由其它调用方选择设上限并收尾。
"""

from __future__ import annotations

import asyncio
import difflib
import time
from dataclasses import replace
from typing import Any, Callable, List, NamedTuple, Tuple

from lls.ai import (
    AgentEvent,
    AgentEventKind,
    ConfigError,
    default_catalog,
)
from lls.ai.sessions import (
    SessionBusy,
    SessionError,
    default_budget,
    store,
)
from lls.ai.tool_options import ToolOptions
from lls.ai.tools import (
    RepeatReminder,
    ScreenView,
    Toolbox,
    builtin_tools,
    teacher_tools,
)
from lls.ai.tools.policy import ToolPolicy, global_policy, reset_global_policy
from lls.ai.prompt import HELP as TEACHER_HELP
from lls.ai.prompt import build as build_prompt
from lls.ai.turn import CompactFailed, Turn, TurnResult, TurnState
from lls.commands.catalog import CommandMeta, completer_for
from lls.commands.result import CommandResult
from lls.commands.spec import command
from lls.config import load as load_config
from lls.offload import offload
from lls.session.errors import Interrupted, SessionEnded
from lls.session.services import Services
from lls.ui import (
    SGR_RESET,
    clusters,
    conservative_char_width,
    strip_sgr,
    text_width,
    wrap_multi_lines,
)

from ..keys import Aborted, SessionClosed, poll_keys
from .agent_pager import page
from .agent_picker import pick
from .tool_picker import ToolSwitch, toggle_tools

__all__ = ["cmd_agent"]

# ---- 常量 ----

# 命令模式读行时的返回哨兵：Esc 退出、Ctrl-C 清行、Ctrl-D 退出
_ESCAPE = object()
_CANCEL = object()
_FINISHED = object()  # 这一轮跑完了：执行中的输入行该收了
_COLLAPSE = object()  # 输入行空了：收起来，屏幕回到单行执行界面
_EXIT = object()
_CONTINUE = object()

PAINT_INTERVAL = 0.1      # 状态覆盖层刷新间隔
LOG_ENTRIES = 2           # 会话提示里显示最近几条
ALL_TURNS = 10 ** 9       # /status 翻看时取全部轮次

MODEL_ROWS_KEPT = 6       # 会话提示上方至少留几行给子进程画面
SUGGESTED_REPLY_LINES = 6 # 只用于提示词：建议的答复长度，显示层不再按行截断

# ---- 会话内斜杠命令 ----
# 名字、别名、处理方法、一句话说明；/help 与「未知命令」的提示都由它生成：
# 加一条命令只需在这里加一行，再写一个 ``_slash_<名字>`` 方法。
class _SlashCommand(NamedTuple):
    """会话内命令：名字、别名、处理方法、说明、行尾语法提示。"""

    name: str
    aliases: Tuple[str, ...]
    method: str
    summary: str
    usage: str


_SLASH_COMMANDS = (
    _SlashCommand("help", ("h", "?"), "_slash_help", "列出这些命令", "/help"),
    _SlashCommand("status", ("s",), "_slash_status", "整屏翻看全部对话", "/status"),
    _SlashCommand(
        "title", (), "_slash_title", "查看或修改标题：/title [文字]", "/title [文字]"
    ),
    _SlashCommand(
        "model",
        (),
        "_slash_model",
        "查看或切换模型：/model [provider/model]",
        "/model [provider/model]",
    ),
    _SlashCommand(
        "compact", (), "_slash_compact", "立刻压到底：只留最后一步，其余进摘要", "/compact"
    ),
    _SlashCommand("tools", (), "_slash_tools", "列出模型可用的工具", "/tools"),
    _SlashCommand(
        "sessions",
        ("ls", "resume"),
        "_slash_sessions",
        "选一条会话切过去：/sessions",
        "/sessions",
    ),
    _SlashCommand("new", (), "_slash_new", "开一条新会话并切过去：/new [标题]", "/new [标题]"),
    _SlashCommand(
        "fork", (), "_slash_fork", "从当前会话分叉：/fork [--at <seq>]", "/fork [--at <seq>]"
    ),
    _SlashCommand(
        "rm", (), "_slash_rm", "删除本会话（要确认），随后挑一条接着用", "/rm"
    ),
    _SlashCommand(
        "archive", (), "_slash_archive", "归档本会话（要确认），随后挑一条接着用", "/archive"
    ),
    _SlashCommand("retry", (), "_slash_retry", "接着本轮继续执行：/retry", "/retry"),
    _SlashCommand(
        "stay",
        (),
        "_slash_stay",
        "持续执行：一轮结束直接接着跑，直到再 /stay 或 Ctrl-C",
        "/stay [on|off]",
    ),
    _SlashCommand(
        "undo", (), "_slash_undo", "撤销上一轮，并把那句话放回输入行", "/undo"
    ),
    _SlashCommand("exit", ("quit", "q"), "_slash_exit", "退出 agent 模式", "/exit"),
)

_SLASH_INDEX = dict(
    [(item.name, item.method) for item in _SLASH_COMMANDS]
    + [
        (alias, item.method)
        for item in _SLASH_COMMANDS
        for alias in item.aliases
    ]
)

#: 补全表：与 /help、_SLASH_INDEX 共用同一份数据（redis 的命令表也兼着这个）。
_SLASH_METAS = tuple(
    CommandMeta(
        name=item.name,
        aliases=item.aliases,
        usage=item.usage,
        summary=item.summary,
    )
    for item in _SLASH_COMMANDS
)

#: 会话内输入行的补全器：只认行首的 ``/命令``，路径与 ``//`` 原样放过。
_SLASH_COMPLETER = completer_for(_SLASH_METAS, marker="/", path_escape=True)

# 执行中输入这条命令怎么办。只在「有一轮正跑着、用户又输入了命令」时才看（见
# _typing）：判定集中在这一张表里，命令方法自己不知道忙不忙。四种：
#   now   —— 不碰本轮正在用的东西：就地执行，完了回到执行中界面
#   after —— 只动历史尾部：排到本轮结束后执行（提示里写明）
#   idle  —— 会顶掉本轮的立足点：执行中拒绝，并说明先 Ctrl-C
#   abort —— 必须立刻结束本轮（/exit）
# 新增一条命令必须在这里也写一行：test_agent_session 里有测试兜着。
_SLASH_POLICY = {
    "_slash_help": "now",
    "_slash_status": "now",
    "_slash_title": "now",
    "_slash_model": "now",
    "_slash_tools": "now",
    "_slash_compact": "after",
    "_slash_sessions": "idle",
    "_slash_new": "idle",
    "_slash_fork": "idle",
    "_slash_rm": "idle",
    "_slash_archive": "idle",
    "_slash_retry": "idle",
    "_slash_stay": "now",
    "_slash_undo": "idle",
    "_slash_exit": "abort",
}


class _Switch(Exception):
    """换会话：交给 ``cmd_agent`` 的循环重开一条。

    ``target`` 是 ``("open", 会话 id)`` 或 ``("new", 标题)``。换会话要换
    provider、预算和事件流，比在 ``_Session`` 里原地改干净。
    """

    def __init__(self, target):
        super(_Switch, self).__init__(target)
        self.target = target


def _looks_like_path(name):
    """``usr/bin/env``、``etc/passwd`` 是路径而非命令，不应作为命令处理。"""
    return "/" in name or name.startswith((".", "~"))


def _unknown_command(name):
    """没匹配上的 ``/xxx``：报错，不当任务发出去。

    拼写错误的 ``/modle`` 若悄悄变成任务，模型会拿它当指令去执行，风险远高于一句报错；而报错是可恢复的 —— 这行已经进了输入历史，↑ 就能改。
    """
    rows = ["未知命令：/{}（这一行未发送给模型）".format(name)]
    close = difflib.get_close_matches(
        name, sorted(item.name for item in _SLASH_COMMANDS), n=1, cutoff=0.6
    )
    if close:
        rows.append("是否想输入 /{}？".format(close[0]))
    rows.append("/help 列出全部命令。")
    rows.append("如需将这一行原样发送给模型，请在行首再加一个斜杠：//{}".format(name))
    return "\n".join(rows)


# 这一族的名字：提示行、任务行、状态行都用它。
NAME = "agent"
INPUT_PROMPT = "(" + NAME + "): "
# 执行中的输入行：这一行回车不是「下一轮」，是排在这一轮后面（见 _typing）。
QUEUE_PROMPT = "(" + NAME + "-queued): "
# 行首一个斜杠：这一行是命令，不会发给模型。提示符当场换成它，输入第一个
# 字符就能看出「回车是执行命令」而不是「回车是下一轮任务」。
CMD_PROMPT = "(" + NAME + "-cmd): "
TASK_PREFIX = "(" + NAME + ") "   # 任务行：与 /status 里的一字不差
REPLY_PREFIX = "(agent-reply) "
REPLY_BOLD = "\x1b[1m"      # 最近一条答复加粗显示
NOTE_PREFIX = "(agent-note) "

def _system_prompt(tools: Any, *, teacher: bool) -> str:
    """按这一刻真正可用的工具拼提示词。

    工具段由工具清单生成：被策略禁用、或宿主没装上的工具不会出现在提示词里，
    模型不会去调一个不存在的工具。教师工具（``ask_teacher``）只有装配了它时
    才在末尾追加一段。
    """
    return build_prompt(
        tools,
        display="interactive",
        mode="task",
        reply_lines=SUGGESTED_REPLY_LINES,
        log_entries=LOG_ENTRIES,
    ) + (TEACHER_HELP if teacher else "")


# ---- 单行/折叠显示 ----

def _width(text: str) -> int:
    """按保守宽度量一段文字（模糊宽度字符按两列算，SGR 序列不算宽）。"""
    return text_width(text, conservative_char_width)


def _flat(text: str) -> str:
    """压成一行（换行、制表、连续空白都按一个空格处理）。"""
    cleaned = text.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    return " ".join(cleaned.split())


def _clip(text: str, columns: int) -> str:
    """按保守宽度硬截断到 ``columns`` 列，不补省略号。"""
    if columns <= 0:
        return ""
    flat = _flat(text)
    if _width(flat) <= columns:
        return flat

    kept: list[str] = []
    used = 0
    for cluster in clusters(flat):
        width = conservative_char_width(cluster)
        if used + width > columns:
            break
        kept.append(cluster)
        used += width
    return "".join(kept)


def _line(prefix: str, text: str, columns: int) -> str:
    """拼一条带前缀的单行，保证整行不超过终端宽度（超出截断末尾保留开头）。"""
    room = max(1, columns - _width(prefix) - WIDTH_SLACK)
    return prefix + _clip(text, room)


def _tail_line(prefix: str, text: str, columns: int) -> str:
    """拼一条带前缀的单行，超宽时保留**末尾**。

    思考流是持续追加的：读的人关心最新一段。按结尾硬截断（不补省略号），
    预览才会随流式输出往前滚动，而不是停在最早那一段。
    """
    room = max(1, columns - _width(prefix) - WIDTH_SLACK)
    flat = _flat(text)
    if _width(flat) <= room:
        return prefix + flat

    kept: list[str] = []
    used = 0
    for cluster in reversed(list(clusters(flat))):
        width = conservative_char_width(cluster)
        if used + width > room:
            break
        kept.append(cluster)
        used += width
    return prefix + "".join(reversed(kept))


def _reply_rows(text: str, columns: int, bold: bool = False) -> list[str]:
    """把标签和答复拼在一起后再按终端宽度折行。

    续行不再缩进：悬挂缩进会白占一大块列宽（答复一半以上都是缩进就太亏了）。
    首尾换行与空行一律丢掉：模型常在答复末尾补一个换行，保留下来就会多出
    一条空行；空行还会挤占只有几行的显示预算。

    ``bold`` 为真时只给答复正文加粗（标签保持原样），便于在连着的几轮里
    一眼认出最新那条。
    """
    clean = text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ")
    clean = clean.strip("\n")
    body = REPLY_BOLD + clean + SGR_RESET if bold and clean else clean
    wrapped, _ = wrap_multi_lines(
        REPLY_PREFIX + body,
        max(1, columns),
        end="\n",
        measure=conservative_char_width,
    )
    # 空行只剩 SGR 序列，按可见内容判断才不会把空行留下。
    return [row for row in wrapped.split("\n") if strip_sgr(row).strip()]


# 第一行没有具体动作时说的话：每一句都对应一个能核对的时刻。
PREPARING = "preparing..."      # 快照、拼上下文：此刻没有请求在路上
COMPACTING = "compacting..."    # 把旧历史压成摘要，一次网络请求
AWAITING = "awaiting reply..."  # 请求已发出，模型第一个字节还没到
REPLYING = "replying..."        # 正文在流里
REASONING = "reasoning..."      # 只有推理在流里，正文还没开始
PLANNING = "planning a call..." # 工具调用的参数在流里，还没拼完

# 单行留两列余量：比终端窄一点也不会折行。
WIDTH_SLACK = 2

# ---- 状态覆盖层 ----

class _Painter:
    """把循环进度折成一行状态文本；没人打字时自己把它画在屏幕上。

    执行中的屏幕有两个主人，交接点是用户按下的第一个键：在那之前屏幕上
    只有这一行状态 —— 与执行中输入行出现之前的样子一模一样，用户没在打字，
    就不该先摆一条提示行占着他看输出的地方。一旦按了键，屏幕交给输入行
    （见 :meth:`_Session._running_text`），这里退成只算一行文本（``line``）。
    同一时刻只有一个写者，两边不会互相踩。
    """

    def __init__(self, services: Services, stay: Callable[[], bool] | None = None) -> None:
        self._s = services
        self._stay = stay if stay is not None else (lambda: False)
        self._step = 0
        self._phase = PREPARING
        self._action = ""
        self._think = ""
        self._reply = ""
        self._call = ""
        self._call_name = ""
        self._started = 0.0
        self._painted = 0.0
        self._line = ""
        self._drawn = ""
        self._draw = True   # 自己画；交给输入行之后只算文本
        self._calling = False

    @property
    def line(self) -> str:
        """当前的状态行；第一帧还没算出来时是空串。"""
        return self._line

    def begin(self) -> None:
        """立刻摆上这一轮的第一帧，不等模型回话。

        提示行在回车时已经收起，第一次 STEP 事件却要等首字节回来（前头还有
        压缩、拼上下文这些要联网的步骤）。中间那一段状态行不能空着 ——
        看着就像程序卡住了。
        """
        self._phase = PREPARING
        self.refresh(force=True)

    def compacting(self) -> None:
        """压缩开始：一次摘要请求，可能要好几秒，得让屏幕说出来。"""
        self._phase = COMPACTING
        self.refresh(force=True)

    def awaiting(self) -> None:
        """请求马上发出：先摆上这一相，别停在上一相上冒充当前状态。"""
        self._phase = AWAITING
        self.refresh(force=True)

    def on_event(self, event: AgentEvent) -> None:
        if event.kind is AgentEventKind.STEP:
            self._step = event.step
            # 新的一步：上一段的调用、参数、推理都成过去了，留在屏上只会
            # 冒充当前状态。
            self._action = ""
            self._call = ""
            self._call_name = ""
            self._think = ""
            self._reply = ""
            self._phase = AWAITING
            self._calling = False
            self.refresh(force=True)
        elif event.kind is AgentEventKind.TEXT:
            # 正文开始了，推理是过去式；正文本身也要像推理一样跟着流走。
            self._think = ""
            self._reply = (self._reply + event.text)[-400:]
            if self._phase != REPLYING:
                self._phase = REPLYING
                self.refresh(force=True)
            else:
                self.refresh()
        elif event.kind is AgentEventKind.REASONING:
            self._think = (self._think + event.text)[-400:]
            if self._phase == AWAITING:
                self._phase = REASONING
                self.refresh(force=True)
            else:
                self.refresh()
        elif event.kind is AgentEventKind.CALL_DELTA:
            if event.name:
                self._call_name = event.name
            self._call = (self._call + event.text)[-400:]
            self._phase = PLANNING
            self._calling = False
            self.refresh()
        elif event.kind is AgentEventKind.CALL:
            self._started = time.monotonic()
            self._calling = True
            self._action = ("calling " + event.name + ": " + event.text)[-400:]
            self._call = ""
            self.refresh(force=True)
        elif event.kind is AgentEventKind.RESULT:
            # 细节在终端画面里，这里只要「跑完了、花了多久」。
            self._calling = False
            self._action = "{} done ({:.1f}s)".format(
                event.name, time.monotonic() - self._started
            )
            self.refresh(force=True)

    def refresh(self, force: bool = False) -> None:
        """重算状态行。``force`` 时不看节流，状态换了就一定跟手。"""
        now = time.monotonic()
        if not force and now - self._painted < PAINT_INTERVAL:
            return
        self._painted = now
        columns = max(1, self._s.screen.columns)
        action = self._action
        if not action and self._call:
            # 参数还在流里：先把「正在拼哪个调用、拼到哪」显示出来。
            action = (self._call_name + " " + self._call).strip()
        # 还没开始第一步时不写「第 0 步」：那会儿只是把状态占上，
        # 数字要等真正开始算才出现。
        prefix = "({}) ".format(NAME)
        if self._step:
            prefix = "({} {}) ".format(NAME, self._step)
        if action and self._calling:
            # 工具可能跑几十秒，中间没有任何事件：让这一行自己往前走。
            action = "{} · {:.1f}s".format(action, now - self._started)
        if action:
            line = _line(prefix, action, columns)
        elif self._reply:
            # 正文流和推理一样贴着状态词滚动；只保留最后一段，避免一行太长。
            line = _tail_line(prefix + self._phase + " ", self._reply, columns)
        elif self._think:
            # 推理不另占一行：接在当前这一相后面，超宽时留末尾 —— 读的人关心
            # 最新想到哪。状态词短，让它留在前头，否则这行只剩没头没尾的碎字。
            line = _tail_line(prefix + self._phase + " ", self._think, columns)
        else:
            phase = self._phase
            if self._stay():
                # 持续执行时状态行必须说出来，否则用户不知道它为什么一直动。
                phase += " · stay"
            line = _line(prefix, phase, columns)
        self._line = line
        if self._draw and line != self._drawn:
            # 定时重画的多数帧一行未变：不重写屏幕，避免终端重复刷新。
            self._drawn = line
            self._s.draw_overlay(line)

    def hand_over(self) -> None:
        """把屏幕交给输入行：此后只算这一行文本，不再自己画。"""
        self._draw = False

    def take_back(self) -> None:
        """输入行空了收起来：屏幕还给自己，从头画一遍单行执行界面。"""
        self._draw = True
        self._drawn = ""


# ---- 会话 ----

def _hands_over(line: Any) -> bool:
    """这一行进来后会立刻换成另一个覆盖层吗？

    斜杠命令不交给模型：它们要么自己画屏（``/status`` 的翻看、``/help``
    的消息框），要么下一轮提示原地重画。成了任务的行也立刻换成状态覆盖层
    （见 :meth:`_Painter.begin`）。两种都别先把提示占的行还给子进程：那一
    帧还原会闪一下空白，紧接着又画回来。
    """
    return isinstance(line, str)


class _Parsed(NamedTuple):
    """一行输入解析出来的东西：先看清要做什么，排队和兑现都用它。

    执行中输入的那一行不能把原文塞进队里、到兑现时再解释一遍 —— 那就成了
    两套说法（排的是这一行，跑的也许是另一行）。所以一按回车就整行解释掉，
    队里放的是这条解释的结果。
    """

    kind: str        # task / command / unknown / exit / skip / end
    shown: str = ""  # 屏幕上原样显示的那一行（排队列表用）
    text: str = ""   # kind=="task"：交给模型的那一句
    name: str = ""   # kind=="command"：要调的方法；unknown：输入的名称
    args: str = ""


def _parse(line: Any) -> "_Parsed":
    """一行输入 → 要做什么。纯解析：不动状态、不读键、不碰网络。"""
    if line is None:
        return _Parsed("end")
    if line is _CANCEL:
        return _Parsed("skip")
    if line is _ESCAPE or line is _EXIT:
        return _Parsed("exit")
    text = str(line).strip()
    if not text:
        return _Parsed("skip")
    if text.lower() in ("/exit", "/quit", "exit", "quit"):
        return _Parsed("exit", text)
    if not text.startswith("/"):
        return _Parsed("task", text, text)
    if text.startswith("//"):
        # 转义：去掉一个斜杠，原样交给模型。
        return _Parsed("task", text, text[1:])
    name, _, rest = text[1:].partition(" ")
    name = name.lower()
    if not name:
        # 只打了个 /：直接给命令表。
        return _Parsed("command", text, name="_slash_help")
    method = _SLASH_INDEX.get(name)
    if method is None:
        if _looks_like_path(name):
            # /usr/bin/env、/etc/passwd 这类是路径，不应作为命令处理。
            return _Parsed("task", text, text)
        return _Parsed("unknown", text, name=name)
    return _Parsed("command", text, name=method, args=rest.strip())


def _is_command_line(text: str) -> bool:
    """这一行回车是执行命令，而不是发给模型？

    提示符换不换、↑ 翻哪一类历史，用的都是这一条：``//转义`` 与
    ``/usr/bin/env`` 这类路径仍算任务，拼写错误的命令归为命令 —— 改一个字母再
    回车，本来就是翻命令的用处。
    """
    return _parse(text).kind in ("command", "unknown")


def _history_kind(text: str) -> str:
    """这一行属于哪一类历史：``/命令`` 与任务各存一份，↑ 各翻各的。"""
    return "cmd" if _is_command_line(text) else "task"


class _Session:
    """一个持续的 Agent 会话：多轮任务共享历史，每轮内部自动多步执行。

    历史来自 ``Session``（事件流折叠出来的视图）；本轮发生的每条消息都立刻
    追加成事件，因此中断、崩溃都只影响「还没说的那部分」。
    """

    def __init__(
        self,
        services: Services,
        provider: Any,
        model: str,
        session: Any = None,
        *,
        budget: int = 0,
        provider_id: str = "",
        catalog: Any = None,
        keeper: Any = None,
    ) -> None:
        self._s = services
        self._provider = provider
        self._model = model
        self._provider_id = provider_id
        self._catalog = catalog  # /model 要解析模型名并构造 provider
        self._keeper = keeper    # 会话类命令借用命令层手里的仓库
        self._session = session
        self._budget = max(0, int(budget))
        self._view = ScreenView(services, self._poll)
        self._guard = RepeatReminder()
        # 两层策略：会话层只含 meta.json 里显式写过的部分，
        # 与全局（进程级）逐字段合并后才是这条会话实际生效的设置。
        self._session_policy = ToolPolicy.from_json(
            session.meta.tools if session is not None else {}
        )
        self._policy = global_policy().merged(self._session_policy)
        self._all_tools = builtin_tools(services, self._poll, view=self._view)
        self._teacher: Any = None
        # 一轮任务之间共享的状态（请求信封指纹、教师转录、最近窗口），
        # 归 Turn 读写，命令层只在显示时读。
        self._turn_state = TurnState()
        config = load_config()
        teacher_model = getattr(config.ai, "teacher_model", None) if config.ai else None
        if teacher_model and catalog is not None:
            try:
                teacher_provider, teacher_name = catalog.resolve(teacher_model)
            except Exception:  # noqa: BLE001 - 教师配置坏了不该挡住会话
                teacher_provider = None
                teacher_name = ""
            if teacher_provider is not None:
                self._teacher = teacher_provider
                self._all_tools.extend(
                    teacher_tools(
                        teacher_provider,
                        teacher_name,
                        lambda: self._turn_state.transcript,
                    )
                )
        self._tools = Toolbox(self._policy.filter(list(self._all_tools)))
        self._tools.add_middleware(self._guard)
        self._log = session.log(LOG_ENTRIES) if session is not None else []
        self._retype = ""  # /undo 放回输入行的原文，读过一次就清空
        self._draft = ""   # 执行中输入中的草稿：回到提示时原样还给你
        self._queue: List[_Parsed] = []   # 执行中回车排下的队，按顺序兑现
        self._notice = ""             # 执行中「已排队」那一句，画在队伍上方
        self._abort: Any = None  # 执行中的中断信号，见 _poll（每轮新建）
        # 这一轮是否占着键盘：从等第一下按键起，到输入行收工。占着的时候
        # 键盘归这一轮自己读（见 _listen），_poll 不再去抢。
        self._typing_active = False
        self._end = False              # 这一轮里子进程没了，见 _poll
        self._exit_requested = False   # 执行中输入了 /exit：这一轮收尾后退
        self._stay = False             # /stay：每轮结束后直接接着跑，见 run()
        self._turn_no = 0

    async def run(self, first_task: str | None) -> bool:
        """进入会话；返回 True 表示用户主动退出，False 表示子进程结束。

        /sessions 选完会话这类命令抛 ``_Switch``，由 ``cmd_agent`` 接住重开一条。
        """
        task: Any = first_task.strip() if first_task else None
        while True:
            if self._exit_requested:
                return True
            if task is None:
                # 执行中排下的队先兑现：一轮结束后直接接着跑队首，
                # 队空了才回到提示。排队的内容仍按一行的规矩解释 ——
                # 斜杠命令仍然是命令，不是任务。
                if self._queue:
                    task = await self._interpret(self._queue.pop(0))
                elif self._stay:
                    # 持续执行：没有新任务就一直跑。没有可续的一轮时这就是
                    # 一次空历史请求（模型看到的只有系统提示与终端画面）。
                    task = _CONTINUE
                else:
                    task = await self._ask()
                if task is _EXIT:
                    return True
                if task is None:
                    return False
                if task is _CANCEL or not task:
                    task = None
                    continue
            try:
                await self._one(task)
            except SessionClosed:
                return False
            # 下一轮从「有没有排队、要不要持续执行」重新判断：两者都由
            # 循环开头那一处决定，这里不再替它选。
            task = None

    async def _ask(self) -> Any:
        """读一个任务；返回文本或 ``_CANCEL`` / ``_EXIT`` / None（会话结束）。"""
        line = await self._s.read_line(
            # 提示串用函数：输入第一个字符就得换成 (agent-cmd)，见 _input_prompt。
            self._prompt_text,
            include_last=False,
            value=self._retype or self._draft,
            cancel=_CANCEL,
            exit=_EXIT,
            history_id="agent_task",
            history_scope="session",
            history_kind=_history_kind,
            escape=_ESCAPE,
            completer=_SLASH_COMPLETER,
            handoff=_hands_over,
            on_edit=self._remember_draft,
        )
        # /undo 放回来的原文、执行中没发出去的草稿都只回填这一次：
        # 读过就清空，下一行提示不再带着它们。
        self._retype = ""
        self._draft = ""
        return await self._interpret(_parse(line))

    async def _interpret(self, parsed: "_Parsed") -> Any:
        """解析结果 → 任务 / ``_CANCEL`` / ``_EXIT`` / None（会话结束）。

        空闲的提示与执行中的队列共用这一套兑现：队里放的是解释好的
        :class:`_Parsed`（见 :func:`_parse`），到了该跑的时候只剩执行 ——
        斜杠命令仍然是命令，不是任务。
        """
        if parsed.kind == "end":
            return None
        if parsed.kind == "exit":
            return _EXIT
        if parsed.kind == "skip":
            return _CANCEL
        if parsed.kind == "task":
            return parsed.text
        if parsed.kind == "unknown":
            await self._s.show_message(_unknown_command(parsed.name))
            return _CANCEL
        try:
            return await getattr(self, parsed.name)(parsed.args)
        except SessionClosed:
            return None

    # ---- 各个 / 命令 ----

    async def _slash_help(self, args: str) -> Any:
        rows = ["会话内命令（本地执行，不发送给模型）："]
        for item in _SLASH_COMMANDS:
            alias = ""
            if item.aliases:
                alias = " ({})".format("、".join("/" + name for name in item.aliases))
            policy = _SLASH_POLICY.get(item.method, "")
            summary = item.summary
            if policy == "idle":
                summary += "（执行中不可用）"
            elif policy == "after":
                summary += "（本轮结束后执行）"
            rows.append("  {}{}  {}".format(item.usage, alias, summary))
        rows.append("行首 // 表示去掉一个斜杠后原样发送给模型；/usr/bin 这类路径也原样发出。")
        await self._s.show_message("\n".join(rows))
        return _CANCEL

    async def _slash_exit(self, args: str) -> Any:
        return _EXIT

    async def _slash_status(self, args: str) -> Any:
        """``/status``：整屏翻看全部对话，排版与输入时的提示一致。"""
        rows = await offload(self._transcript)
        footer = await offload(self._status_footer)
        await page(self._s, rows, footer)
        return _CANCEL

    def _transcript(self) -> List[str]:
        """整条会话的对话与备注，只用 ``(agent)`` 那三种前缀。"""
        if self._session is None:
            return [NOTE_PREFIX + "本次会话不保存到磁盘，没有可翻看的历史"]
        rows = self._entry_rows(self._session.log(ALL_TURNS))
        return rows or [NOTE_PREFIX + "还没有对话"]

    def _entry_rows(self, entries: Any) -> List[str]:
        """按输入提示的版式摊开若干轮：任务一行，答复折行，备注一行。"""
        columns = max(1, self._s.screen.columns)
        latest = -1
        for index, (_, reply, _) in enumerate(entries):
            if reply:
                latest = index
        rows: List[str] = []
        for index, (task, reply, note) in enumerate(entries):
            rows.append(_line(TASK_PREFIX, task, columns))
            if reply:
                rows.extend(_reply_rows(reply, columns, bold=index == latest))
            if note:
                rows.append(_line(NOTE_PREFIX, note, columns))
        return rows

    def _status_footer(self) -> str:
        """末行唯一的一条状态：标题、模型、窗口用量。"""
        if self._session is None:
            return "不保存 · {} · 预算 {} tok".format(self._model, self._budget)
        meta = self._session.meta
        window = self._turn_state.window or self._session.window(self._budget)
        return " · ".join(
            [
                _flat(meta.title) or meta.id,
                self._model,
                "{}/{} tok".format(window.used, window.budget),
            ]
        )

    async def _slash_title(self, args: str) -> Any:
        if self._session is None:
            await self._s.show_message("本次会话不持久化，无法修改标题")
            return _CANCEL
        text = args.strip()
        if not text:
            await self._s.show_message(
                "标题：{}（使用 /title <文字> 修改）".format(
                    self._session.meta.title or "无标题"
                )
            )
            return _CANCEL
        self._session.set_title(text)
        await self._s.show_message("标题已更新为 " + self._session.meta.title)
        return _CANCEL

    async def _slash_model(self, args: str) -> Any:
        """查看或切换模型：/model [provider/model]"""
        spec = args.strip()
        if spec:
            return await self._switch_model(spec)
        return await self._pick_model()

    def _model_choices(self) -> List[Tuple[str, str]]:
        """配置里列出的全部模型：``[(provider/model, 标签)]``。"""
        rows: List[Tuple[str, str]] = []
        if self._catalog is None:
            return rows
        for provider_id in self._catalog.provider_ids():
            try:
                provider = self._catalog.provider_config(provider_id)
            except Exception:  # noqa: BLE001 - 单个 provider 坏掉不该断了整张表
                continue
            for model_id in provider.models:
                spec = "{}/{}".format(provider_id, model_id)
                rows.append((spec, spec))
        return rows

    async def _pick_model(self) -> Any:
        """无参数：弹出选择器；没有可选项时退回文本提示。"""
        choices = self._model_choices()
        if not choices:
            rows = [
                "当前模型：{}（provider {}）".format(
                    self._model, self._provider_id or "-"
                )
            ]
            if self._catalog is not None and self._catalog.provider_ids():
                rows.append("已配置 provider：" + "、".join(self._catalog.provider_ids()))
            rows.append("没有可选的模型（ai.providers.<id>.models 为空）")
            rows.append("用法：/model <provider/model>")
            await self._s.show_message("\n".join(rows))
            return _CANCEL
        if self._typing_active:
            await self._s.show_message("本轮正在执行，结束后才能切换模型")
            return _CANCEL
        current = "{}/{}".format(self._provider_id, self._model)
        specs = [spec for spec, _ in choices]
        # 与 /sessions 一样用 * 标当前；名字列对齐。
        width = max(len(spec) for spec in specs)
        labels = [
            "{} {}".format("*" if spec == current else " ", spec.ljust(width))
            for spec in specs
        ]
        start = specs.index(current) if current in specs else 0
        chosen = await pick(
            self._s,
            specs,
            labels,
            cursor=start,
            header="选择模型（↑↓ 移动，回车切换）：",
        )
        if chosen is None:
            return _CANCEL
        return await self._switch_model(chosen)

    async def _switch_model(self, spec: str) -> Any:
        """切到 provider/model；失败只报错，不改状态。"""
        if self._catalog is None:
            await self._s.show_message("本次会话无法读取模型配置，无法切换模型")
            return _CANCEL
        try:
            provider_id, model_id = self._catalog.resolve_spec(spec)
            provider = self._catalog.get(provider_id)
        except (ConfigError, SessionError) as e:
            await self._s.show_message("错误：{}".format(e))
            return _CANCEL
        self._provider = provider
        self._provider_id = provider_id
        self._model = model_id
        self._budget = _budget_for(self._catalog, provider_id)
        if self._session is not None:
            meta = self._session.meta
            meta.provider_id = provider_id
            meta.model_id = model_id
            self._session.append_model_switch(meta.model)
            if self._keeper is not None:
                self._keeper.write_meta(meta)
        await self._s.show_message(
            "已切换到 {}/{}（窗口 {} tok），历史不变，下一轮生效".format(
                provider_id, model_id, self._budget
            )
        )
        return _CANCEL

    async def _slash_compact(self, args: str) -> Any:
        """立刻压一次历史，压到底。摘要是一次网络请求：先摆出这一相，且可中断。"""
        if self._session is None:
            await self._s.show_message("本次会话不持久化，没有可压缩的历史")
            return _CANCEL
        painter = _Painter(self._s)
        # 这时没人打字：这一行状态自己画上去，压完由 finally 收尾。
        painter.compacting()
        turn = self._make_turn()
        try:
            compressed, after, reason = await turn.compact_now()
        except (Aborted, CompactFailed) as e:
            await self._s.show_message("{}，历史没有改动".format(e))
            return _CANCEL
        finally:
            self._s.restore_context()
        if compressed:
            await self._s.show_message("已压缩，当前窗口 {}".format(after.describe()))
        else:
            await self._s.show_message("{}；当前窗口 {}".format(reason, after.describe()))
        return _CANCEL

    async def _slash_tools(self, args: str) -> Any:
        """工具开关：空格翻转，r 回到跟随全局，回车保存到本会话。"""
        if self._typing_active:
            await self._s.show_message("本轮正在执行，结束后才能修改工具")
            return _CANCEL
        names = [entry.tool.name for entry in self._all_tools]
        if not names:
            await self._s.show_message("没有可用工具")
            return _CANCEL
        switches = [
            ToolSwitch(
                name=name,
                enabled=self._policy.options(name).enabled is not False,
                source="session" if name in self._session_policy else "global",
                fallback=global_policy().enabled(name),
            )
            for name in names
        ]
        chosen = await toggle_tools(
            self._s,
            switches,
            header="工具开关（空格切换，r 跟随全局，Enter 保存到本会话）",
        )
        if chosen is None:
            return _CANCEL
        self._apply_tool_switches(chosen)
        off = [item.name for item in chosen if not item.enabled]
        if off:
            await self._s.show_message("已保存到本会话：已关闭 " + "、".join(off))
        else:
            await self._s.show_message("已保存到本会话：全部启用")
        return _CANCEL

    def _apply_tool_switches(self, switches: "list[ToolSwitch]") -> None:
        """把界面结果写回会话层；下一轮请求立即用新工具集。

        ``source == "global"`` 表示这一项跟随全局：从会话层删掉它。
        """
        for item in switches:
            if item.source == "global":
                self._session_policy.set_options(item.name, ToolOptions())
            elif item.enabled:
                self._session_policy.enable(item.name)
            else:
                self._session_policy.disable(item.name)
        self._policy = global_policy().merged(self._session_policy)
        self._tools = Toolbox(self._policy.filter(list(self._all_tools)))
        self._tools.add_middleware(self._guard)
        if self._session is not None and self._keeper is not None:
            self._session.meta.tools = self._session_policy.to_json()
            self._keeper.write_meta(self._session.meta)

    async def _slash_sessions(self, args: str) -> Any:
        """列出全部会话，光标上下选，回车切过去。"""
        if self._keeper is None:
            await self._s.show_message("会话存储不可用")
            return _CANCEL
        current = self._session.id if self._session is not None else ""
        chosen = await _choose_session(self._s, self._keeper, current)
        if chosen is None or chosen.id == current:
            return _CANCEL
        raise _Switch(("open", chosen.id))

    def _find_meta(self, spec: str) -> Any:
        """解析会话 id / 前缀 / 标题片段：返回 ``(meta, 错误文本)`` 之一。"""
        try:
            return self._keeper.resolve(spec), ""
        except SessionError as e:
            return None, "错误：{}".format(e)

    async def _slash_new(self, args: str) -> Any:
        raise _Switch(("new", args.strip()))

    async def _slash_fork(self, args: str) -> Any:
        if self._keeper is None or self._session is None:
            await self._s.show_message("本次会话不持久化，无法分叉")
            return _CANCEL
        at_seq = 0
        rest = args.strip()
        if rest:
            head, _, tail = rest.partition(" ")
            if head != "--at" or not tail.strip().isdigit():
                await self._s.show_message("用法：/fork [--at <seq>]")
                return _CANCEL
            at_seq = int(tail.strip())
        try:
            forked = self._keeper.fork(self._session.id, at_seq)
        except SessionError as e:
            await self._s.show_message("错误：{}".format(e))
            return _CANCEL
        await self._s.show_message(
            "已分叉到 {}，来自会话 {}{}".format(
                forked.id,
                self._session.id,
                "（到 seq {} 为止）".format(at_seq) if at_seq else "（全部事件）",
            )
        )
        raise _Switch(("open", forked.id))

    async def _confirm(self, question: str) -> bool:
        """问一句 y/N，只有明确答 y 才返回 True。

        什么都不输入、按 Ctrl-C、或者答了别的话，一律当作「不」：这里问的
        都是要动手删除、归档的事，默认必须是不动它。
        """
        answer = await self._s.read_line(
            question + " [y/N] ",
            cancel="",
            history_id="agent-confirm",
            keep_history=False,
        )
        return isinstance(answer, str) and answer.strip().lower() in ("y", "yes")

    async def _switch_after_gone(self, note: str) -> Any:
        """本会话刚被删掉或归档：把剩下的会话摆出来接着用，不直接退出。

        ``note`` 是「已删除 / 已归档」那一句，放在选择界面顶上。挑中的那条
        由 :class:`_Switch` 交给 ``cmd_agent`` 重开；按 q、子进程退出、或者
        一条会话都不剩时才 ``_EXIT`` —— 那时确实没有可接着用的上下文。
        """
        if self._keeper is not None and self._keeper.list():
            chosen = await _choose_session(
                self._s,
                self._keeper,
                header=note + "挑一条接着用：",
                footer="up/down: select, Enter: switch, q: leave agent",
            )
            if chosen is not None:
                raise _Switch(("open", chosen.id))
            await self._s.show_message(note + "已退出 agent 模式。")
            return _EXIT
        await self._s.show_message(
            note + "没有其他会话，已退出 agent 模式；新建会话请使用 agent --new。"
        )
        return _EXIT

    async def _slash_rm(self, args: str) -> Any:
        """删掉本会话：确认后删，然后把会话列表摆出来让人挑下一条。

        只认自己，不带参数 —— 会话 id 记不住，也不该为了删一条会话先
        ``/exit``。删掉的正是「不想留」的那一条，所以不会一并新建空会话：
        选界面里挑中的那条才接着用，按 q、或者一条都不剩时才退出 agent。
        """
        if args.strip():
            await self._s.show_message("用法：/rm（删除本会话，不接受参数）")
            return _CANCEL
        if self._keeper is None:
            await self._s.show_message("会话存储不可用")
            return _CANCEL
        if self._session is None:
            await self._s.show_message("本次会话不持久化，没有可删除的会话")
            return _CANCEL
        meta = self._session.meta
        if not await self._confirm(
            "删除会话 {}（{}）？删除后无法恢复".format(meta.id, meta.title or "无标题")
        ):
            return _CANCEL
        try:
            gone = self._keeper.drop(meta.id)
        except SessionError as e:
            await self._s.show_message("错误：{}".format(e))
            return _CANCEL
        return await self._switch_after_gone(
            "已删除会话 {}（{}）。".format(gone.id, gone.title or "无标题")
        )

    async def _slash_archive(self, args: str) -> Any:
        """把本会话收进归档，然后把会话列表摆出来让人挑下一条。

        归档不是删除：目录移进 ``_archive/``，默认列表里不再出现，
        ``agent --all`` 仍然看得到。收起来的那条不再接着用，但也不必因此
        退出 agent —— 挑一条留着接着用，按 q 或一条都不剩时才退出。
        """
        if args.strip():
            await self._s.show_message("用法：/archive（归档本会话，不接受参数）")
            return _CANCEL
        if self._keeper is None:
            await self._s.show_message("会话存储不可用")
            return _CANCEL
        if self._session is None:
            await self._s.show_message("本次会话不持久化，没有可归档的会话")
            return _CANCEL
        meta = self._session.meta
        if not await self._confirm(
            "把会话 {}（{}）移入归档？".format(meta.id, meta.title or "无标题")
        ):
            return _CANCEL
        try:
            gone = self._keeper.archive(meta.id)
        except SessionError as e:
            await self._s.show_message("错误：{}".format(e))
            return _CANCEL
        return await self._switch_after_gone(
            "已归档会话 {}（{}）。".format(gone.id, gone.title or "无标题")
        )

    async def _slash_retry(self, args: str) -> Any:
        """接着本轮往下执行，等同于这一轮还没结束。不新增任何消息。

        没有可接着跑的一轮（新会话、还没提过任务）时直说，别拿空历史去问
        模型：那一轮既没有任务也没有答复，回来只会把提示行又刷一遍。
        """
        if not self._log:
            await self._s.show_message("本轮已经结束，没有可继续执行的内容")
            return _CANCEL
        return _CONTINUE

    async def _slash_stay(self, args: str) -> Any:
        """开关持续执行：一轮结束不再回到提示符，直接接着跑。

        不带参数是切换，``on``/``off`` 显式。与 ``lls serve --stay`` 是同一段
        循环；差别只在交互式这边两轮之间先去兑现排队的任务。

        ``Ctrl-C`` 会把它关掉（见 :meth:`_one`）—— 只中断不倒计时的话，用户
        按一次停不下来。
        """
        want = args.strip().lower()
        if want not in ("", "on", "off"):
            await self._s.show_message("用法：/stay [on|off]")
            return _CANCEL
        self._stay = want == "on" if want else not self._stay
        await self._s.show_message(
            "持续执行：开（再 /stay 或 Ctrl-C 关掉）"
            if self._stay
            else "持续执行：关"
        )
        return _CANCEL

    async def _slash_undo(self, args: str) -> Any:
        """``/undo``：抹掉上一轮，连同这一轮期间写下的压缩。

        只动视图：事件文件里的原文一个字不删，所以连按几次就一直往前退。
        撤掉的那句话放回输入行（``read_line`` 的 ``value``），改一改回车即可
        重发。折成一行是输入行的限制 —— 它只有一行，硬塞换行会把覆盖层的高度
        算错，反而摆不下。
        """
        if args.strip():
            await self._s.show_message("用法：/undo（撤销上一轮，不接受参数）")
            return _CANCEL
        if self._session is None:
            if not self._log:
                await self._s.show_message("没有可撤销的轮次")
                return _CANCEL
            task = self._log.pop()[0]
        else:
            point = self._session.undo_point()
            if point is None:
                await self._s.show_message("没有可撤销的轮次")
                return _CANCEL
            start, task = point
            self._session.append_undo(start - 1)
            self._log = self._session.log(LOG_ENTRIES)
        self._turn_state.window = None
        rows = [row.strip() for row in task.splitlines() if row.strip()]
        self._retype = " ".join(rows)
        note = "已撤销上一轮，那句话放回输入行（改完回车就重发）"
        if len(rows) > 1:
            note += "\n原来是 {} 行，已折成一行".format(len(rows))
        await self._s.show_message(note)
        return _CANCEL

    async def _one(self, task: Any) -> None:
        """执行一轮任务。异常路径只影响本轮，不影响已经写下的事件。

        ``task`` 是 ``_CONTINUE`` 时接着现有会话往下执行，不新增消息。

        屏幕在这一轮里分成两段（见 :meth:`_listen`）：用户没按键时只有那一
        行状态，和以前一模一样；按了第一个键才换成输入行 —— 一边看它跑一边
        输入下一句并回车即可排入队列（见 :meth:`_typing`）。
        """
        painter = _Painter(self._s, stay=lambda: self._stay)
        # 先摆上状态，再去忙那些要等的事（快照、压缩、拼上下文都在 Turn 里）：
        # 等下去的那几秒屏幕上不能是空的。
        painter.begin()
        self._guard.reset()
        self._turn_no += 1
        # 本轮的步号、压缩与修剪标记由 Turn 在开跑时自己清空。
        self._notice = ""
        self._end = False
        # 事件在协程里建：模块级构造时还没有事件循环（会拿到旧的那个）。
        self._abort = asyncio.Event()
        work = asyncio.ensure_future(self._turn(task, painter))
        typed = asyncio.ensure_future(self._listen(painter, task, work))
        try:
            await typed
        finally:
            if not work.done():
                work.cancel()
            outcome = await asyncio.gather(work, return_exceptions=True)
            # 这一轮的覆盖层到此为止：收起它，把子进程画面放回来。
            self._s.restore_context()
        error = outcome[0]
        if isinstance(error, Aborted):
            # 中断也可能发生在请求之前（裁历史、压缩）：这一轮同样只是被放弃。
            # Ctrl-C 是「停下来」，不是「歇一轮」：持续执行也一并关掉。
            self._stay = False
            self._record("", "", str(error))
        elif isinstance(error, BaseException) and not isinstance(
            error, asyncio.CancelledError
        ):
            raise error

    async def _listen(self, painter: "_Painter", task: Any, work: Any) -> None:
        """执行中这一段的屏幕：单行 → 完整（按了键）→ 空了再回单行。

        这块屏只有两张脸。完整界面（历史 + 任务行 + 状态 + 排队 + 输入行）
        空闲和执行中都一样能用，差别只在输入行提示：空闲 ``(agent)``，执行中
        ``(agent-queued)``，后者说明这一行回车是排队；行首一个斜杠时提示换成
        ``(agent-cmd)``，说明这一行是命令（空闲、执行中都一样；见
        :meth:`_input_prompt`）。单行界面只有那一行状态，
        是这一轮**默认**的长相：没人打字，屏幕上就不该先摆一条提示行占着他
        看输出的地方。

        交接点是第一个按键，往回也是同一个条件 —— 输入行一空（回车排完队、
        或者退格退光）就收起来。按键一个都不丢：等的那一段只看看有没有，
        不消费（见 :meth:`_watch`），输入行接手后按原顺序处理。
        """
        # 先占上键盘：_poll 靠这个标志让路，等第一下按键的那一段也不能
        # 让两个协程同时读 stdin（中间那一瞬也一样）。
        self._typing_active = True
        try:
            while True:
                if not await self._watch(painter, work):
                    return
                painter.hand_over()
                if await self._typing(painter, task, work):
                    return
                # 输入行空着收起来了：屏幕还给状态行，回到单行执行界面。
                painter.take_back()
                painter.refresh(force=True)
        finally:
            self._typing_active = False

    async def _watch(self, painter: "_Painter", work: Any) -> bool:
        """等第一下按键（输入行收起之后也是等这个）：返回 True 就该摊开屏幕。

        这一段只做两件事：按 ``PAINT_INTERVAL`` 重画那一行状态（工具跑几十
        秒时它得自己往前走），以及看看键盘上有没有动静。Ctrl-C 照旧当场
        放弃这一轮；其余按键攒着不动，留给接手的那一屏。
        """
        while not work.done():
            try:
                await self._s.wait(PAINT_INTERVAL)
            except Interrupted:
                self._abort.set()
                continue
            except SessionEnded:
                # 子进程没了：_turn 自己按「会话结束」收尾，这里不再读键盘，
                # 等它写完这一步再交差（先走会让这一轮被当成取消）。
                self._end = True
                while not work.done():
                    await asyncio.sleep(0)
                return False
            if self._s.keys_pending():
                return True
            painter.refresh()
        return False

    async def _typing(self, painter: "_Painter", task: Any, work: Any) -> bool:
        """执行中的输入行：边跑边收，回车要么排队要么就地执行，Esc/Ctrl-C 只弃这一轮。

        回车那一行先整行解释（见 :func:`_parse`），再按 ``_SLASH_POLICY`` 分派：
        不碰本轮的就地执行（``now``），只动历史尾部的排到本轮之后（``after``），
        会顶掉本轮立足点的拒绝并说明先 Ctrl-C（``idle``），``/exit`` 让这一轮
        收尾后退（``abort``）。任务照旧排队。

        返回 True 表示这一轮到头了（离开 :meth:`_listen`）；False 表示输入行
        空了，该收起它、回到单行执行界面。

        走到这里说明用户已经按过键了（见 :meth:`_listen`）：这是完整界面在
        执行中的样子，不是这一轮的默认长相。与空闲提示用的是同一个编辑器、
        同一个 ``history_id``：↑ 召回、多行粘贴、emoji 退格全都一样。草稿一
        输入后即交给 :meth:`_remember_draft` 存着，所以中断之后回到提示，那句话
        还在原处，不会白打一遍。
        """

        def tick() -> Any:
            """空转一格：刷新状态行，看这一轮完没完、输入行该不该收了。"""
            painter.refresh()
            if work.done():
                return _FINISHED
            if not self._draft and not self._s.keys_pending():
                # 没有输入、手上也没有按键：收起输入行，回单行执行界面。
                return _COLLAPSE
            return None

        def keep(line: Any) -> bool:
            # 轮次结束、收起输入行，都要把那一帧还给子进程；其余结局下一格
            # 立刻又画回来，那一帧还原只会闪一下空白。
            return line is not _FINISHED and line is not _COLLAPSE

        while True:
            result = await self._s.read_line(
                lambda: self._running_text(painter, task),
                include_last=False,
                value=self._draft,
                cancel=_CANCEL,
                exit=_EXIT,
                history_id="agent_task",
                history_scope="session",
                history_kind=_history_kind,
                escape=_ESCAPE,
                completer=_SLASH_COMPLETER,
                handoff=keep,
                tick=tick,
                tick_interval=PAINT_INTERVAL,
                on_edit=self._remember_draft,
            )
            if result is _FINISHED:
                return True
            if result is None:
                # 子进程没了：这一轮按「会话结束」收尾（_turn 会记下来），
                # 这里只等它跑完，不再读输入 —— 读也是立刻返回，空转。
                self._end = True
                while not work.done():
                    await asyncio.sleep(0)
                return True
            if result is _COLLAPSE:
                return False
            if result is _CANCEL or result is _ESCAPE or result is _EXIT:
                # Ctrl-C / Esc / Ctrl-D：只放弃这一轮。草稿还在就留在编辑器
                # 里接着改；输入行空了就收起来。
                self._abort.set()
                if self._draft:
                    continue
                return False
            text = str(result).strip()
            self._draft = ""
            if not text:
                return False
            parsed = _parse(text)
            policy = _SLASH_POLICY.get(parsed.name, "") if parsed.kind == "command" else ""
            if policy == "now":
                # 就地执行：翻看、看状态、改标题、换模型都不碰本轮正在用的
                # 东西。跑完接着读这一行；一轮照旧在后台跑，回到 _watch 时
                # 屏幕还是「这一轮在跑」的样子。
                await self._interpret(parsed)
                continue
            if policy == "abort":
                # /exit：先让这一轮收尾落盘，再退 —— 不把它扔在半路。
                self._abort.set()
                while not work.done():
                    await asyncio.sleep(0)
                self._exit_requested = True
                return True
            if policy == "idle":
                # 会顶掉本轮的立足点。拒绝，但把这一行留在编辑器里，并说清
                # 下一步怎么做：不是默默无视。
                self._draft = text
                await self._s.show_message(
                    "本轮正在执行，请先按 Ctrl-C 中断，再执行 {}。".format(text)
                )
                continue
            if parsed.kind == "unknown":
                self._draft = text
                await self._s.show_message(_unknown_command(parsed.name))
                continue
            # 排到这一轮后面：任务如此，/compact 这种只动历史尾部的命令也
            # 如此。队里放的是解释好的那一条，兑现时只管执行。
            self._queue.append(parsed)
            self._notice = "已排队，本轮结束后执行：{}".format(text) if policy else ""
            return False

    def _remember_draft(self, text: str, cursor: int) -> None:
        """执行中输入中的草稿：中断之后回到提示时原样还给你。"""
        self._draft = text

    async def _poll(self, timeout: float) -> None:
        """等网络时不再读键盘：这一轮里键盘归 :meth:`_watch` / 输入行，这里
        只等中断信号（Ctrl-C 由那边置上 ``_abort``）。

        空闲路径（``/compact``、``/retry``）没有别人在读键盘，仍按老办法等：
        那时 Ctrl-C 必须当场生效。
        """
        if not self._typing_active:
            # 空闲路径没有别人在读键盘：仍按老办法等，Ctrl-C 当场生效。
            await poll_keys(self._s, timeout)
            return
        if self._end:
            raise SessionClosed("会话已结束")
        if self._abort.is_set():
            raise Aborted("已中断")
        try:
            await asyncio.wait_for(self._abort.wait(), timeout)
        except asyncio.TimeoutError:
            return
        raise Aborted("已中断")

    async def _turn(self, task: Any, painter: "_Painter") -> None:
        """一轮任务的主体。覆盖层的生死由 :meth:`_one` 负责。"""
        result = await self._make_turn(painter).run(
            None if task is _CONTINUE else task
        )
        if isinstance(result.stopped, SessionClosed):
            # 子进程没了：先写 turn/end，再把「会话结束」交给 _one 收场。
            self._finish_turn(replace(result, note="子进程退出"))
            raise result.stopped
        self._finish_turn(result)
        if result.status == "ok":
            self._record(result.label, result.text)
        else:
            self._record(result.label, "", result.note)

    def _make_turn(self, painter: _Painter | None = None) -> Turn:
        """按当前模型、工具与预算装配一轮；界面行为以回调交回命令层。"""

        def on_phase(phase: str) -> None:
            if painter is None:
                return
            if phase == "compacting":
                painter.compacting()
            elif phase == "awaiting":
                painter.awaiting()

        # 教师提示只在 ask_teacher 真的装配着时才写：工具可能被策略关掉。
        teacher = any(entry.tool.name == "ask_teacher" for entry in self._tools)
        return Turn(
            services=self._s,
            view=self._view,
            provider=self._provider,
            model=self._model,
            session=self._session,
            tools=self._tools,
            budget=self._budget,
            provider_id=self._provider_id,
            catalog=self._catalog,
            system=_system_prompt(self._tools, teacher=teacher),
            state=self._turn_state,
            turn=self._turn_no,
            poll=self._poll,
            on_event=painter.on_event if painter is not None else None,
            on_phase=on_phase,
        )

    # ---- 记录 ----

    def _finish_turn(self, result: TurnResult) -> None:
        """收尾：写 turn/end，并刷新覆盖层的任务列表。"""
        if self._session is None:
            return
        detail = "；".join(part for part in (result.note, result.turn_note) if part)
        self._session.append_turn_end(
            status=result.status,
            note=detail,
            usage=result.usage,
            turn=self._turn_no,
        )
        self._log = self._session.log(LOG_ENTRIES)

    def _record(self, task: str, reply: str, note: str = "") -> None:
        """覆盖层用的两行；有会话时它就是事件流折出来的最后两条。

        ``task`` 为空表示这是接着跑的那一段（``/retry``），没有新的任务行：
        另起一条同名任务，和 :func:`log_entries` 折事件时一样，别留下一条
        空任务的行，也别把中断前那句答复顶掉。
        """
        if self._session is not None:
            self._log = self._session.log(LOG_ENTRIES)
            return
        if task:
            self._log.append((task, reply, note))
        else:
            self._log.append((self._log[-1][0] if self._log else "", reply, note))
        self._log = self._log[-max(1, LOG_ENTRIES):]

    def _overlay_text(self, rows: List[str], prompt: str) -> str:
        """把上方那几行与输入行拼成这一屏的提示串。

        高度只由屏幕实际行数决定，不再有固定上限：覆盖层比屏幕高就会贴
        顶，多出来的行被挤到屏幕外，终端随之滚动 —— 物理行与模型行错位。
        所以约束只有两条：上面留几行给子进程画面，以及（连同行尾的提示
        符）整体不越过屏幕。矮终端上这两条自然把预算收紧。

        放不下时丢**最早**的那几行：离输入行最近的先留着，最新的话都在
        眼前。
        """
        limit = max(3, self._s.screen.rows - MODEL_ROWS_KEPT)
        limit = min(limit, max(1, self._s.screen.rows - 1))
        if len(rows) > limit:
            rows = rows[-limit:]
        if not rows:
            return prompt
        return "\n".join(rows) + "\n" + prompt

    def _prompt_text(self) -> str:
        """空闲态的提示：最近几轮对话 + 输入行。"""
        return self._overlay_text(
            self._entry_rows(self._log), self._input_prompt(INPUT_PROMPT, CMD_PROMPT)
        )

    def _input_prompt(self, task_prompt: str, cmd_prompt: str) -> str:
        """这一行是任务还是命令：行首一个斜杠就是命令（``//`` 是转义）。

        提示符跟着换，输入第一个字符就看出回车会执行命令、不会发给模型。
        执行中也不换：命令就是命令，不因这一轮还在跑就另起一个提示符。
        """
        if _is_command_line(self._draft):
            return cmd_prompt
        return task_prompt

    def _running_text(self, painter: "_Painter", task: Any) -> str:
        """执行中的提示：对话 + **在跑的那一行** + 状态 + 排队 + 输入行。

        在跑的那一行写成 ``(agent) <任务>``，与这一轮落盘、进 /status 的那一
        行一字不差：轮次结束时屏幕不会跳一下，只是它下面那两行收了。
        """
        columns = max(1, self._s.screen.columns)
        rows = self._entry_rows(self._log)
        label = "" if task is _CONTINUE else str(task)
        if label:
            rows.append(_line(TASK_PREFIX, label, columns))
        if painter.line:
            rows.append(painter.line)
        if self._notice:
            rows.append(_line(NOTE_PREFIX, self._notice, columns))
        total = len(self._queue)
        for index, item in enumerate(self._queue, 1):
            rows.append(
                _line(
                    NOTE_PREFIX,
                    "排队 {}/{}: {}".format(index, total, item.shown),
                    columns,
                )
            )
        return self._overlay_text(rows, self._input_prompt(QUEUE_PROMPT, CMD_PROMPT))


# ---- 命令 ----

class _Options:
    """``agent`` 的参数：与命令行风格一致，但由命令自己解析。"""

    def __init__(self) -> None:
        self.new = False
        self.list_only = False
        self.all = False
        self.resume: str | None = None
        self.title: str | None = None
        self.model: str | None = None
        self.task: str | None = None


def _parse_args(args: str | None) -> _Options:
    """拆出 ``--new`` / ``--list`` / ``--resume`` / ``--title`` / ``--model``。"""
    opts = _Options()
    rest = (args or "").strip()
    while rest.startswith("--"):
        head, _, tail = rest.partition(" ")
        if head == "--new":
            opts.new = True
            rest = tail.strip()
        elif head == "--list":
            opts.list_only = True
            rest = tail.strip()
        elif head == "--all":
            opts.list_only = True
            opts.all = True
            rest = tail.strip()
        elif head in ("--resume", "--model"):
            value, _, tail = tail.strip().partition(" ")
            if not value:
                raise ValueError("{} 需要提供值".format(head))
            if head == "--resume":
                opts.resume = value
            else:
                opts.model = value
            rest = tail.strip()
        elif head == "--title":
            # 标题可能带空格：整行剩下的都算标题。
            if not tail.strip():
                raise ValueError("--title 需要提供文本")
            opts.title = tail.strip()
            rest = ""
        else:
            raise ValueError("未知参数 {}".format(head))
    opts.task = rest or None
    return opts


def _session_label(meta: Any, current: bool = False, busy: bool = False) -> str:
    """一条会话一行：定宽字段在前，标题在最后。

    只有标题长短不定，放在末尾其余各列才对得齐；超宽由调用方裁掉。行首两个
    字符的标记：``*`` 是当前会话，``!`` 是另一个进程正开着。
    """
    marker = ("*" if current else " ") + ("!" if busy else " ")
    return "{} {}  {:>3} 轮  {:>4.0f}KB  {}  {}".format(
        marker,
        meta.id,
        meta.turns,
        meta.size / 1024.0,
        _stamp(meta.updated_at),
        meta.title or "（无标题）",
    )


async def _choose_session(
    services: Any,
    keeper: Any,
    current: str = "",
    busy: str = "",
    footer: str = "",
    header: str = "选择会话（↑↓ 移动，回车切换）：",
) -> Any:
    """把全部会话摆出来让人选，返回选中的 meta。

    光标停在 ``current`` 上；``busy`` 只加一个 ``!`` 标记 —— 被另一个进程占着
    的会话仍然可以被选中，那是用户自己的决定。
    """
    metas = await offload(keeper.list)
    if not metas:
        await services.show_message("尚没有会话；运行 agent 可新建一条")
        return None
    labels = [
        _session_label(meta, meta.id == current, meta.id == busy) for meta in metas
    ]
    start = next((n for n, meta in enumerate(metas) if meta.id == current), 0)
    extra = {} if not footer else {"footer": footer}
    return await pick(services, metas, labels, cursor=start, header=header, **extra)


def _stamp(value: float) -> str:
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(value))
    except (OverflowError, OSError, ValueError):
        return "-"


def _open_or_create(opts: _Options, catalog: Any, keeper: Any) -> Any:
    """按参数取回（或新建）要进入的会话。"""
    session = None
    if not opts.new:
        spec = opts.resume if opts.resume else None
        session = keeper.open(spec)
    provider_id = model_id = ""
    if opts.model:
        provider_id, model_id = catalog.resolve_spec(opts.model)
    if session is None:
        if not provider_id:
            provider_id, model_id = catalog.resolve_spec()
        return keeper.create(provider_id, model_id, title=opts.title or "")
    if opts.model and (session.meta.provider_id, session.meta.model_id) != (
        provider_id,
        model_id,
    ):
        session.meta.provider_id = provider_id
        session.meta.model_id = model_id
        session.append_model_switch("{}".format(session.meta.model))
        keeper.write_meta(session.meta)
    if opts.title:
        session.set_title(opts.title)
    keeper.set_current(session.id)
    return session


def _budget_for(catalog: Any, provider_id: str) -> int:
    try:
        conf = catalog.provider_config(provider_id)
    except Exception:  # noqa: BLE001 - 配置缺失时用默认窗口
        return default_budget(128_000)
    return default_budget(conf.context_window, catalog.context_limit())


@command("agent", aliases=("a",), category="ai", usage="agent [任务]")
async def cmd_agent(s: Services, args: str | None) -> CommandResult:
    """进入交互式 Agent 会话，按任务自动多轮执行"""
    try:
        opts = _parse_args(args)
    except ValueError as e:
        await s.show_message("错误：{}".format(e))
        return CommandResult()

    keeper = store()
    if opts.list_only:
        metas = keeper.list(include_archived=opts.all)
        await s.show_message(
            "\n".join([_session_label(m) for m in metas])
            or "尚没有会话；运行 agent 可新建一条"
        )
        return CommandResult()

    try:
        catalog = default_catalog()
    except ConfigError as e:
        await s.show_message("错误：{}".format(e))
        return CommandResult()

    # 全局工具策略从配置重算（--set 改过的也算数）；会话层在 _Session 里叠加。
    reset_global_policy(catalog.tools())

    task = opts.task
    exited = False
    while True:
        # 进不去这条会话时先记下原因，随后把列表摆出来让人换一条：
        # 占用、--resume 指错、这条会话的 provider 没了，都是换一条就能继续的。
        why = ""
        busy = ""
        session = None
        try:
            session = _open_or_create(opts, catalog, keeper)
        except SessionBusy as e:
            why, busy = "{}".format(e), e.session_id
        except SessionError as e:
            if not opts.resume:
                await s.show_message("错误：{}".format(e))
                return CommandResult()
            why = "错误：{}".format(e)
        except ConfigError as e:
            # 模型 / provider 配置本身有问题，换会话也没用。
            await s.show_message("错误：{}".format(e))
            return CommandResult()
        else:
            try:
                provider = catalog.get(session.meta.provider_id)
            except ConfigError as e:
                gone = session.id
                session.close()
                session = None
                why = "错误：{}（会话 {} 的 provider 已不存在）".format(e, gone)
        if why:
            await s.show_message(why)
            chosen = await _choose_session(
                s,
                keeper,
                keeper.current_id(),
                busy=busy,
                header=(
                    "选择会话（↑↓ 移动，回车切换）："
                    if not busy
                    else "会话 {} 正被另一个进程占用，请另选一条：".format(busy)
                ),
            )
            if chosen is None:
                return CommandResult()
            opts.new = False
            opts.resume = chosen.id
            opts.title = None
            opts.model = None
            continue
        assert session is not None

        budget = _budget_for(catalog, session.meta.provider_id)
        worker = _Session(
            s,
            provider,
            session.meta.model_id,
            session,
            budget=budget,
            provider_id=session.meta.provider_id,
            catalog=catalog,
            keeper=keeper,
        )
        target = None
        exited = False
        try:
            exited = await worker.run(task)
        except _Switch as e:
            target = e.target
        finally:
            keeper.write_meta(session.meta)
            session.close()
        if target is None:
            break
        # 用户换了会话：按目标重开一条，回到循环开头。任务算已交付，不再重放。
        kind, value = target
        opts.new = kind == "new"
        opts.resume = value if kind == "open" else None
        opts.title = value if kind == "new" else None
        opts.model = None
        task = None
    return CommandResult(handler="default" if exited else None)
