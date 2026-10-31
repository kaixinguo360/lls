"""交互式 agent 会话：每轮任务自动执行，结束后回到同一会话提示。

覆盖会话状态机（提示、历史、日志、退出）、工具行为、取消与失败路径，
以及不限步数的长任务。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os

import pytest

from lls.ai import Chunk, ChunkKind, ConfigError, ToolCall, ToolCallDelta
from lls.commands import get_command, keys
from lls.commands.builtin import agent as agent_cmd
from lls.ui import strip_sgr
from lls.ai.sessions import SessionStore
from lls.ai.tools import RepeatReminder, ScreenView, builtin_tools
from lls.ai.tools import shell as shell_mod
from lls.ai.sessions.events import INTERRUPT_MARKER
from lls.session.errors import Interrupted, SessionEnded
from lls.tty import KeyNameError

# ---- 桩 ----


class FakeScreen:
    columns = 80
    rows = 24
    alternate = False

    def __init__(self, text: str = "screen", texts=None, increment: bool = False) -> None:
        self._text = text
        self._texts = list(texts) if texts is not None else None
        self._increment = increment
        self._calls = 0
        self.limits = []

    def text(self, **kwargs) -> str:
        self.limits.append(kwargs.get("limit"))
        self._calls += 1
        if self._increment:
            return f"line-{self._calls}"
        if self._texts is not None:
            index = min(self._calls - 1, len(self._texts) - 1)
            return self._texts[index]
        return self._text


class FakeServices:
    def __init__(self, answers=(), screen=None) -> None:
        self.screen = screen or FakeScreen()
        self._answers = list(answers)
        self.prompts = []
        self.written = []
        self.painted = []
        self.messages = []
        self.values = []  # 每次读输入时输入区里预填着什么（/undo 回填原文）
        self._chars = []
        self.held = []

    def queue_chars(self, *values) -> None:
        self._chars.extend(values)

    async def read_line(self, prompt, **kwargs):
        tick = kwargs.get("tick")
        if tick is not None:
            return await self._running_input(prompt, kwargs)
        # 提示串可以是函数（空闲提示也要跟着草稿换 (agent-cmd)）：记下现算的那一帧。
        self.prompts.append(prompt() if callable(prompt) else prompt)
        self.values.append(kwargs.get("value"))
        return self._answers.pop(0) if self._answers else None

    async def _running_input(self, prompt, kwargs):
        """执行中的输入行：空转到这一轮结束，或交出一段中途敲下的东西。

        真实引擎在这里等键、按帧重画。测试里没敲键就是不敲：于是它只剩
        「每一格看一下轮次结束没有」。queue_chars 排进来的才是中途敲的：
        b"\x03" 是 Ctrl-C，其余当成敲完回车的一行。
        """
        cancel = kwargs.get("cancel")
        on_edit = kwargs.get("on_edit")
        for _ in range(200000):
            frame = prompt() if callable(prompt) else prompt
            if self.held:
                # 等第一下按键时 ``wait`` 已经顺手指到的那几个：按到达的
                # 顺序交给这个输入行，不能因为换了读法就丢掉。
                self._chars[0:0] = self.held
                self.held.clear()
            if not self.painted or self.painted[-1] != frame:
                self.painted.append(frame)
            if self._chars:
                value = self._chars.pop(0)
                if value is None:
                    return None          # 子进程没了：读输入当场返回 None
                if value == b"\x03":
                    return cancel
                if isinstance(value, str):
                    # 只敲了一半、还没回车：草稿留在输入行里，继续等。
                    if on_edit is not None:
                        on_edit(value, len(value))
                    continue
                text = value.decode()
                if on_edit is not None:
                    on_edit(text, len(text))
                return text
            await asyncio.sleep(0)
            control = kwargs["tick"]()
            if control is not None:
                return control
        raise AssertionError("执行中的输入行一直等不到这一轮结束")

    def keys_pending(self) -> bool:
        """键盘上有没有还没人接手的输入：等第一下按键的那一段靠它决定交棒。"""
        return bool(self._chars or self.held)

    async def wait(self, timeout=None) -> None:
        """等一小段键盘：排在队里的那一段就是这时到达的输入。"""
        # 让出事件循环：真实实现会等输入，模型流任务才有机会推进。
        await asyncio.sleep(0)
        if not self._chars:
            return
        value = self._chars.pop(0)
        if value is None:
            raise SessionEnded("会话已结束")
        if value == b"\x03":
            raise Interrupted("已中断")
        # 其余按键属于用户：真实引擎会攒着，等下一次读输入再交出。
        self.held.append(value)

    async def read_key(self):
        await asyncio.sleep(0)
        return None

    def draw_overlay(self, text, cursor=None):
        self.painted.append(text)
        return 0, len(text.split("\n"))

    def flush_model(self) -> None:
        pass

    def restore_context(self) -> None:
        pass

    def requeue_pending_keys(self) -> None:
        pass

    def write(self, data, enter=False) -> None:
        self.written.append((data, enter))

    async def show_message(self, text) -> None:
        self.messages.append(text)


class QuietFakeServices(FakeServices):
    """键盘先静几格：那几格正是「用户还没按键」的默认长相。"""

    def __init__(self, answers, *, quiet_ticks: int) -> None:
        super().__init__(answers)
        self._quiet = quiet_ticks

    def keys_pending(self) -> bool:
        return self._quiet == 0 and super().keys_pending()

    async def wait(self, timeout=None) -> None:
        if self._quiet:
            self._quiet -= 1
            await asyncio.sleep(0)
            return
        await super().wait(timeout)


class SlowProvider:
    """每片都真等一会儿：给「还没人按键」那几格留出时间。"""

    id = "slow"

    def __init__(self, rounds) -> None:
        self.rounds = list(rounds)
        self.requests = []

    async def chat(self, model, messages, **kwargs):
        self.requests.append({"messages": list(messages)})
        for chunk in self.rounds.pop(0):
            await asyncio.sleep(0.05)
            yield chunk


class FakeCatalog:
    def __init__(self, provider, context_window: int = 128_000) -> None:
        self._provider = provider
        self._window = context_window

    def resolve_spec(self, spec=None):
        return "stub", "stub-model"

    def get(self, provider_id):
        return self._provider

    def resolve(self):
        return self._provider, "stub-model"

    def provider_config(self, provider_id):
        class _Conf:
            context_window = self._window

        return _Conf()

    def context_limit(self):
        return 0

    def tools(self):
        return {}


class ScriptedProvider:
    id = "stub"

    def __init__(self, rounds) -> None:
        self.rounds = list(rounds)
        self.requests = []

    async def chat(
        self, model, messages, *, tools=None, parallel_tool_calls=None, **params
    ):
        self.requests.append(
            {
                "model": model,
                "messages": list(messages),
                "tools": tools,
            }
        )
        for chunk in self.rounds.pop(0):
            yield chunk


class FlakyProvider:
    """第一次请求失败，之后正常。"""

    id = "flaky"

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, model, messages, **kwargs):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("boom")
        yield Chunk(ChunkKind.TEXT, "好了")
        yield Chunk(ChunkKind.DONE)


def _text(value: str):
    return [Chunk(ChunkKind.TEXT, value), Chunk(ChunkKind.DONE)]


def _calls(*calls):
    """按 wire 的样子拆成分片：id/name 只在首片，arguments 分两段发。"""
    chunks = []
    for index, call in enumerate(calls):
        cut = len(call.arguments) // 2
        chunks.append(
            Chunk(
                ChunkKind.TOOL_CALL_DELTA,
                fragment=ToolCallDelta(index, call.id, call.name, call.arguments[:cut]),
            )
        )
        if cut < len(call.arguments):
            chunks.append(
                Chunk(
                    ChunkKind.TOOL_CALL_DELTA,
                    fragment=ToolCallDelta(index, "", "", call.arguments[cut:]),
                )
            )
    return chunks + [Chunk(ChunkKind.DONE)]


def _call(call_id: str, name: str, arguments) -> ToolCall:
    raw = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return ToolCall(id=call_id, name=name, arguments=raw)


def _fresh_store(monkeypatch):
    """换一份干净的会话存储：会话要跨 `agent` 调用延续，但不必跨用例。"""
    keeper = SessionStore()
    monkeypatch.setattr(agent_cmd, "store", lambda: keeper)
    return keeper


def _session(
    monkeypatch,
    provider,
    answers,
    *,
    screen=None,
    first_task=None,
    chars=(),
    keeper=None,
):
    services = FakeServices(answers, screen=screen)
    if chars:
        services.queue_chars(*chars)
    monkeypatch.setattr(agent_cmd, "default_catalog", lambda: FakeCatalog(provider))
    if keeper is None:
        _fresh_store(monkeypatch)
    else:
        monkeypatch.setattr(agent_cmd, "store", lambda: keeper)
    result = asyncio.run(agent_cmd.cmd_agent(services, first_task))
    return services, result


# ---- 会话状态机 ----


def test_session_keeps_prompting_after_each_task(monkeypatch) -> None:
    provider = ScriptedProvider([_text("答复1"), _text("答复2")])

    services, result = _session(monkeypatch, provider, ["任务1", "任务2", "/exit"])

    assert result.handler == "default"
    assert len(provider.requests) == 2
    assert "任务1" in (provider.requests[0]["messages"][-1].content or "")
    roles = [message.role for message in provider.requests[1]["messages"]]
    assert roles == ["system", "user", "assistant", "user"]
    assert "任务1" in (provider.requests[1]["messages"][1].content or "")
    # 第二次提示里能看到第一轮的答复
    assert any("答复1" in prompt for prompt in services.prompts)


def test_first_task_argument_runs_before_the_prompt(monkeypatch) -> None:
    provider = ScriptedProvider([_text("完成")])

    services, result = _session(
        monkeypatch, provider, ["/exit"], first_task="首个任务"
    )

    assert len(provider.requests) == 1
    assert "首个任务" in (provider.requests[0]["messages"][-1].content or "")
    assert result.handler == "default"
    assert "完成" in services.prompts[-1]


def test_a_task_can_take_several_tool_rounds(monkeypatch) -> None:
    provider = ScriptedProvider(
        [
            _calls(_call("c1", "send_text", {"text": "pytest -q", "enter": True})),
            _calls(_call("c2", "read_terminal", {"lines": 40, "wait_seconds": 0})),
            _text("全部通过"),
        ]
    )
    screen = FakeScreen("user@host:~$ ")

    services, result = _session(
        monkeypatch, provider, ["跑一下测试", "/exit"], screen=screen
    )

    # 动作写入了子进程；初始画面随任务消息带给模型
    assert services.written == [(b"pytest -q", True)]
    assert "user@host" in (provider.requests[0]["messages"][-1].content or "")
    # 第三轮请求里能看到 read_terminal 的观察（作为工具结果回灌）
    observation = provider.requests[2]["messages"][-1]
    assert observation.role == "tool"
    assert "<terminal_state>" in (observation.content or "")
    # 最终答复进入下一轮提示
    assert any("全部通过" in prompt for prompt in services.prompts)
    assert result.handler == "default"


def test_empty_enter_reprompts_without_running(monkeypatch) -> None:
    provider = ScriptedProvider([])

    services, result = _session(monkeypatch, provider, ["", "/exit"])

    assert provider.requests == []
    assert len(services.prompts) == 2
    assert result.handler == "default"


def test_ctrl_c_at_the_prompt_reprompts(monkeypatch) -> None:
    provider = ScriptedProvider([])

    services, result = _session(
        monkeypatch, provider, [agent_cmd._CANCEL, "/exit"]
    )

    assert provider.requests == []
    assert len(services.prompts) == 2
    assert result.handler == "default"


@pytest.mark.parametrize("answer", ["/exit", "/quit", "exit", "quit"])
def test_exit_keywords_leave_the_session(monkeypatch, answer) -> None:
    provider = ScriptedProvider([])

    services, result = _session(monkeypatch, provider, [answer])

    assert result.handler == "default"
    assert len(services.prompts) == 1


def test_escape_leaves_the_session(monkeypatch) -> None:
    services, result = _session(
        monkeypatch, ScriptedProvider([]), [agent_cmd._ESCAPE]
    )

    assert result.handler == "default"


def test_ctrl_d_leaves_the_session(monkeypatch) -> None:
    services, result = _session(monkeypatch, ScriptedProvider([]), [agent_cmd._EXIT])

    assert result.handler == "default"


def test_child_exit_at_the_prompt_ends_the_session(monkeypatch) -> None:
    services, result = _session(monkeypatch, ScriptedProvider([]), [None])

    assert result.handler is None


# ---- 失败与取消 ----


def test_provider_error_is_reported_and_the_session_continues(monkeypatch) -> None:
    provider = FlakyProvider()

    services, result = _session(monkeypatch, provider, ["任务1", "任务2", "/exit"])

    assert result.handler == "default"
    assert provider.calls == 2
    assert any("error: boom" in prompt for prompt in services.prompts)


def test_ctrl_c_aborts_only_the_current_task(monkeypatch) -> None:
    provider = ScriptedProvider([_text("第一轮"), _text("第二轮")])

    services, result = _session(
        monkeypatch, provider, ["任务1", "任务2", "/exit"], chars=[b"\x03"]
    )

    assert result.handler == "default"
    assert any("已中断" in prompt for prompt in services.prompts)
    # 被中断的一轮留在会话里，末尾是中断标记：下一轮看得见它做到哪一步。
    sent = [message.content or "" for message in provider.requests[-1]["messages"]]
    assert any(text.endswith("<task>\n任务1\n</task>") for text in sent)
    assert INTERRUPT_MARKER in sent


def test_child_exit_during_a_task_ends_the_session(monkeypatch) -> None:
    provider = ScriptedProvider(
        [
            _calls(_call("c", "read_terminal", {"lines": 10, "wait_seconds": 0})),
            _text("never"),
        ]
    )

    services, result = _session(
        monkeypatch, provider, ["任务1"], chars=[None]
    )

    assert result.handler is None


def test_config_error_stays_in_command_mode(monkeypatch) -> None:
    def boom():
        raise ConfigError("no config")

    monkeypatch.setattr(agent_cmd, "default_catalog", boom)
    services = FakeServices([])

    result = asyncio.run(agent_cmd.cmd_agent(services, None))

    assert result.handler is None
    assert services.messages == ["error: no config"]


# ---- 不限步数与历史 ----


def test_long_task_is_not_cut_off(monkeypatch) -> None:
    """默认不限步数：二十轮工具调用后仍然继续，直到模型给出文本答复。"""
    rounds = [
        _calls(
            _call(
                f"c{index}",
                "send_text",
                {"text": f"step-{index}", "enter": False},
            )
        )
        for index in range(20)
    ]
    rounds.append(_text("完成"))
    provider = ScriptedProvider(rounds)

    services, result = _session(monkeypatch, provider, ["长任务", "/exit"])

    assert len(provider.requests) == 21
    assert all(request["tools"] is not None for request in provider.requests)
    assert any("完成" in prompt for prompt in services.prompts)
    assert result.handler == "default"


def test_history_is_trimmed_by_the_token_budget(monkeypatch) -> None:
    """预算按 token 算：装不下的旧轮次整轮丢掉，当前任务一定带上。"""
    provider = ScriptedProvider(
        [_text("答" * 300), _text("答" * 300), _text("答" * 300)]
    )
    monkeypatch.setattr(
        agent_cmd, "default_catalog", lambda: FakeCatalog(provider, 200)
    )
    keeper = SessionStore()
    monkeypatch.setattr(agent_cmd, "store", lambda: keeper)

    services = FakeServices(["任务1", "任务2", "任务3", "/exit"])
    asyncio.run(agent_cmd.cmd_agent(services, None))

    last = provider.requests[-1]["messages"]
    users = [message for message in last if message.role == "user"]
    assert len(users) <= 2
    assert "任务3" in (users[-1].content or "")
    assert all("任务1" not in (message.content or "") for message in users)


def test_a_huge_tool_output_is_trimmed_out_of_the_next_request(monkeypatch) -> None:
    provider = ScriptedProvider(
        [
            _calls(_call("c1", "read_terminal", {"lines": 10})),
            _text("看完了"),
            _text("第二轮"),
        ]
    )
    monkeypatch.setattr(
        agent_cmd, "default_catalog", lambda: FakeCatalog(provider, 400)
    )
    keeper = SessionStore()
    monkeypatch.setattr(agent_cmd, "store", lambda: keeper)
    services = FakeServices(["读一屏", "再读一屏", "/exit"])
    services.screen = FakeScreen("x" * 4000)

    asyncio.run(agent_cmd.cmd_agent(services, None))

    sent = provider.requests[-1]["messages"]
    assert sent[0].role == "system"
    # 巨型的工具输出已经被裁掉，只剩当前这一条任务消息还带着画面。
    assert sum(1 for message in sent if len(message.content or "") > 3000) == 1
    assert sent[-1].role == "user"


# ---- 提示构造 ----


def test_prompt_shows_the_last_reply_and_stays_within_bounds(monkeypatch) -> None:
    services = FakeServices([])
    session = agent_cmd._Session(services, ScriptedProvider([]), "m")
    session._log = [("任务" * 40, "答复" * 60, "备注" * 20), ("任务二", "答复二", "")]

    prompt = session._prompt_text()

    assert "答复" in prompt
    rows = prompt.split("\n")
    # 高度只受屏幕限制：给子进程画面留几行，其余都可给提示用。
    assert len(rows) <= services.screen.rows - agent_cmd.MODEL_ROWS_KEPT + 1
    assert len(rows) <= services.screen.rows
    for row in rows:
        assert agent_cmd._width(row) <= services.screen.columns

def test_prompt_height_follows_the_screen_instead_of_a_fixed_cap() -> None:
    """屏幕越高，提示能用的行越多 —— 不再有 16 行的固定上限。

    上限固定时，长答复在 60 行的终端上也只留 16 行，白白浪费空间；
    现在只按实际行数收紧。
    """
    tall = FakeServices([], screen=FakeScreen())
    tall.screen.rows = 60
    short = FakeServices([], screen=FakeScreen())
    short.screen.rows = 24
    heavy = [("任务一", "答复" * 1000, "")]
    rows_tall = agent_cmd._Session(tall, ScriptedProvider([]), "m")
    rows_tall._log = list(heavy)
    rows_short = agent_cmd._Session(short, ScriptedProvider([]), "m")
    rows_short._log = list(heavy)

    tall_rows = len(rows_tall._prompt_text().split("\n"))
    short_rows = len(rows_short._prompt_text().split("\n"))

    assert tall_rows > short_rows, "高屏没有用满"
    assert tall_rows <= tall.screen.rows
    assert short_rows <= short.screen.rows


def test_prompt_never_exceeds_the_screen_on_a_short_terminal() -> None:
    """再矮的终端也不能越界：越界会让终端滚动，物理行与模型行错位。"""
    services = FakeServices([], screen=FakeScreen())
    services.screen.rows = 8
    session = agent_cmd._Session(services, ScriptedProvider([]), "m")
    session._log = [("任务" * 40, "答复" * 60, "")]

    rows = session._prompt_text().split("\n")

    assert len(rows) <= services.screen.rows
    assert rows[-1].startswith(agent_cmd.INPUT_PROMPT)



def test_log_keeps_only_the_last_entries() -> None:
    services = FakeServices([])
    session = agent_cmd._Session(services, ScriptedProvider([]), "m")
    session._log = [("t1", "r1", ""), ("t2", "r2", "")]
    session._record("t3", "r3")

    assert len(session._log) == agent_cmd.LOG_ENTRIES
    assert session._log[-1][0] == "t3"


def test_reply_rows_wrap_with_the_prefix_and_without_hanging_indent() -> None:
    rows = agent_cmd._reply_rows("word " * 12, 30)

    assert rows[0].startswith(agent_cmd.REPLY_PREFIX)
    assert len(rows[0]) > len(agent_cmd.REPLY_PREFIX)
    assert all(agent_cmd._width(row) <= 30 for row in rows)
    assert all(not row.startswith(" ") for row in rows)
    assert not any(row.endswith("…") for row in rows)


def test_reply_rows_are_not_capped_by_height() -> None:
    """答复不再按固定行数截断：有多少行就显示多少行。"""
    reason = "\n".join(f"第{i}行" for i in range(30))

    rows = agent_cmd._reply_rows(reason, 80)

    assert len(rows) == 30
    assert rows[-1] == "第29行"


def test_reply_rows_drop_blank_lines() -> None:
    """模型答复里的空行不显示，也不占那几行预算。"""
    rows = agent_cmd._reply_rows("一\n\n二\n", 80)

    assert rows == ["(agent-reply) 一", "二"]


def test_prompt_has_no_blank_line_before_the_input() -> None:
    services = FakeServices([])
    session = agent_cmd._Session(services, ScriptedProvider([]), "m")
    session._log = [("任务", "答复", "")]

    prompt = session._prompt_text()

    assert strip_sgr(prompt) == "(agent) 任务\n(agent-reply) 答复\n(agent): "


# ---- 最近一条答复加粗 ----


def test_only_the_latest_reply_is_bold() -> None:
    services = FakeServices([])
    session = agent_cmd._Session(services, ScriptedProvider([]), "m")
    session._log = [("任务一", "旧答复", ""), ("任务二", "新答复", "")]

    rows = session._prompt_text().split("\n")

    assert not any("\x1b[1m" in row for row in rows if "旧答复" in row)
    assert any("\x1b[1m" in row and "新答复" in row for row in rows)
    assert all(row.endswith("\x1b[0m") for row in rows if "新答复" in row)


def test_bold_reply_survives_line_width_limit() -> None:
    """加粗不占列宽：折行后每行仍不超过终端宽度，且换行处样式重放。"""
    services = FakeServices([])
    session = agent_cmd._Session(services, ScriptedProvider([]), "m")
    session._log = [("任务", "答复" * 40, "")]

    rows = session._prompt_text().split("\n")

    assert len(rows) > 2
    for row in rows:
        assert agent_cmd._width(row) <= services.screen.columns
    assert all("\x1b[1m" in row and row.endswith("\x1b[0m") for row in rows[1:-1])


# ---- 思考预览：始终显示最新一段 ----


def test_tail_line_hard_cuts_the_front_and_keeps_the_end() -> None:
    line = agent_cmd._tail_line("(t) ", "0123456789", 10)

    assert line.startswith("(t) 6")
    assert line.endswith("6789")
    assert "…" not in line
    assert agent_cmd._width(line) <= 10 - agent_cmd.WIDTH_SLACK


def test_tail_line_keeps_short_text_untouched() -> None:
    assert agent_cmd._tail_line("(t) ", "hi", 10) == "(t) hi"


def test_painter_previews_the_latest_reasoning() -> None:
    services = FakeServices([])
    painter = agent_cmd._Painter(services)

    painter.on_event(
        agent_cmd.AgentEvent(
            agent_cmd.AgentEventKind.REASONING,
            step=1,
            text="START" + "x" * 200 + "END",
        )
    )
    painter.refresh(force=True)

    shown = painter.line
    assert "END" in shown
    assert "START" not in shown
    assert "\n" not in shown


def test_painter_hangs_reasoning_after_the_phase() -> None:
    """推理不单占一行：接在当前这一相后面，覆盖层永远只有一行。"""
    services = FakeServices([])
    painter = agent_cmd._Painter(services)

    painter.on_event(agent_cmd.AgentEvent(agent_cmd.AgentEventKind.STEP, step=1))
    painter.on_event(
        agent_cmd.AgentEvent(
            agent_cmd.AgentEventKind.REASONING, step=1, text="先看看目录"
        )
    )

    shown = painter.line
    assert "\n" not in shown
    assert shown.startswith("({} 1) ".format(agent_cmd.NAME) + agent_cmd.REASONING)
    assert shown.endswith("先看看目录")


def test_painter_drops_reasoning_when_an_action_takes_the_line() -> None:
    """有了具体动作，那一行让给动作：推理是过去式，混进来只会顶掉参数。"""
    services = FakeServices([])
    painter = agent_cmd._Painter(services)
    painter.on_event(agent_cmd.AgentEvent(agent_cmd.AgentEventKind.STEP, step=1))
    painter.on_event(
        agent_cmd.AgentEvent(agent_cmd.AgentEventKind.REASONING, step=1, text="想一想")
    )

    painter.on_event(
        agent_cmd.AgentEvent(
            agent_cmd.AgentEventKind.CALL, step=1, name="bash", text='{"cmd": "ls"}'
        )
    )

    shown = painter.line
    assert "\n" not in shown
    assert "想一想" not in shown
    assert shown.startswith("({} 1) calling bash".format(agent_cmd.NAME))


def test_painter_paints_a_phase_before_the_model_answers() -> None:
    """开跑之前就得有状态行：提示行已收起，首字节回来前屏幕上不能是空的。"""
    services = FakeServices([])
    painter = agent_cmd._Painter(services)

    painter.begin()

    assert painter.line == "({}) preparing...".format(agent_cmd.NAME)


def test_painter_says_what_it_is_doing_at_each_phase() -> None:
    """每一相都对应一件具体的事，不留笼统的等待语。"""
    services = FakeServices([])
    painter = agent_cmd._Painter(services)

    shown = []
    painter.begin()
    shown.append(painter.line)
    painter.compacting()
    shown.append(painter.line)
    painter.awaiting()
    shown.append(painter.line)
    text = agent_cmd.AgentEvent(agent_cmd.AgentEventKind.TEXT, step=1, text="好")
    painter.on_event(text)
    shown.append(painter.line)
    painter.on_event(text)
    painter.refresh(force=True)
    shown.append(painter.line)

    assert shown == [
        "(agent) preparing...",
        "(agent) compacting...",
        "(agent) awaiting reply...",
        "(agent) replying...",
        "(agent) replying...",      # 再来一段正文不再换相
    ]


def test_painter_shows_a_call_and_how_long_it_took() -> None:
    """调用期间显示工具名与已用时间；返回后换成「跑完了、花了多久」。"""
    services = FakeServices([])
    painter = agent_cmd._Painter(services)
    call = agent_cmd.AgentEvent(
        agent_cmd.AgentEventKind.CALL,
        step=2,
        name="read_terminal",
        text='{"lines": 3}',
    )

    shown = []
    painter.on_event(agent_cmd.AgentEvent(agent_cmd.AgentEventKind.STEP, step=2))
    shown.append(painter.line)
    painter.on_event(call)
    shown.append(painter.line)
    painter.on_event(
        agent_cmd.AgentEvent(agent_cmd.AgentEventKind.RESULT, step=2, name="read_terminal")
    )
    shown.append(painter.line)

    assert shown[1].startswith(
        '({} 2) calling read_terminal: {{"lines": 3}}'.format(agent_cmd.NAME)
    )
    assert shown[2].startswith("({} 2) read_terminal done (".format(agent_cmd.NAME))
    assert shown[2].endswith("s)")


def test_painter_forgets_the_previous_step() -> None:
    """新的一步不能还挂着上一步的调用和推理。"""
    services = FakeServices([])
    painter = agent_cmd._Painter(services)
    painter.on_event(
        agent_cmd.AgentEvent(agent_cmd.AgentEventKind.REASONING, step=1, text="想一想")
    )
    painter.on_event(
        agent_cmd.AgentEvent(agent_cmd.AgentEventKind.CALL, step=1, name="bash", text="{}")
    )

    painter.on_event(agent_cmd.AgentEvent(agent_cmd.AgentEventKind.STEP, step=2))

    assert painter.line == "({} 2) awaiting reply...".format(agent_cmd.NAME)


# ---- 提示词与显示预算一致 ----


def test_system_prompt_matches_the_display_budget() -> None:
    prompt = agent_cmd.SYSTEM_PROMPT

    assert f"最多 {agent_cmd.SUGGESTED_REPLY_LINES} 行" in prompt
    assert f"最近 {agent_cmd.LOG_ENTRIES} 轮" in prompt
    assert "不要空行" in prompt
    assert "终端画面就是细节所在" in prompt


# ---- 工具 ----


def _tools(services):
    """装配好的工具箱：与 ``_Session`` 里那一份同一条装配路径。"""
    view = ScreenView(services, services.wait)
    box = builtin_tools(services, services.wait, view=view)
    box.add_middleware(RepeatReminder())
    tools = {entry.tool.name: entry for entry in box}
    return tools, view


def test_send_text_writes_bytes_and_enter() -> None:
    services = FakeServices([])
    tools, _ = _tools(services)

    result = asyncio.run(
        tools["send_text"].run({"text": "ls -l", "enter": True})
    )

    assert services.written == [(b"ls -l", True)]
    assert "已写入" in result
    assert "回车" in result


def test_send_text_rejects_a_non_string() -> None:
    services = FakeServices([])
    tools, _ = _tools(services)

    with pytest.raises(ValueError):
        asyncio.run(tools["send_text"].run({"text": 1, "enter": True}))


def test_send_keys_encodes_named_keys() -> None:
    services = FakeServices([])
    tools, _ = _tools(services)

    result = asyncio.run(tools["send_keys"].run({"keys": ["up", "ctrl-c"]}))

    assert services.written == [(b"\x1b[A\x03", False)]
    assert "up" in result and "ctrl-c" in result


def test_send_keys_rejects_unknown_names() -> None:
    services = FakeServices([])
    tools, _ = _tools(services)

    with pytest.raises(KeyNameError):
        asyncio.run(tools["send_keys"].run({"keys": ["nope"]}))


@pytest.mark.parametrize(
    ("encoding", "data", "expected"),
    [
        ("base64", "aGk=", b"hi"),
        ("hex", "1b5b41", b"\x1b[A"),
    ],
)
def test_send_raw_decodes(encoding: str, data: str, expected: bytes) -> None:
    services = FakeServices([])
    tools, _ = _tools(services)

    asyncio.run(tools["send_raw"].run({"data": data, "encoding": encoding}))

    assert services.written == [(expected, False)]


def test_send_raw_rejects_invalid_encodings() -> None:
    services = FakeServices([])
    tools, _ = _tools(services)

    with pytest.raises(ValueError):
        asyncio.run(tools["send_raw"].run({"data": "!!!", "encoding": "base64"}))
    with pytest.raises(ValueError):
        asyncio.run(tools["send_raw"].run({"data": "zz", "encoding": "hex"}))
    with pytest.raises(ValueError):
        asyncio.run(tools["send_raw"].run({"data": "aGk=", "encoding": "rot13"}))


def test_read_terminal_returns_the_screen_and_status() -> None:
    screen = FakeScreen("hello")
    services = FakeServices([], screen=screen)
    tools, _ = _tools(services)

    result = asyncio.run(
        tools["read_terminal"].run({"lines": 30, "wait_seconds": 0})
    )

    assert "<terminal_state>" in result
    assert "hello" in result
    assert "<status>quiet</status>" in result
    assert screen.limits[-1] == 30


def test_read_terminal_notes_an_unchanged_screen() -> None:
    services = FakeServices([], screen=FakeScreen("hello"))
    tools, _ = _tools(services)
    args = {"lines": 30, "wait_seconds": 0}

    asyncio.run(tools["read_terminal"].run(args))
    second = asyncio.run(tools["read_terminal"].run(args))

    assert "与上次读取相同" in second


def test_read_terminal_waits_for_quiet(monkeypatch) -> None:
    monkeypatch.setattr(agent_cmd, "READ_POLL", 0.001)
    monkeypatch.setattr(agent_cmd, "READ_QUIET", 0.005)
    screen = FakeScreen(texts=["a", "b", "b", "b", "b"])
    services = FakeServices([], screen=screen)
    tools, _ = _tools(services)

    result = asyncio.run(
        tools["read_terminal"].run({"lines": 10, "wait_seconds": 1})
    )

    assert "b" in result
    assert "<status>quiet</status>" in result


def test_read_terminal_reports_still_producing(monkeypatch) -> None:
    monkeypatch.setattr(agent_cmd, "READ_POLL", 0.001)
    monkeypatch.setattr(agent_cmd, "READ_QUIET", 10.0)
    screen = FakeScreen(increment=True)
    services = FakeServices([], screen=screen)
    tools, _ = _tools(services)

    result = asyncio.run(
        tools["read_terminal"].run({"lines": 10, "wait_seconds": 0.02})
    )

    assert "<status>still-producing</status>" in result


def test_read_terminal_aborts_on_ctrl_c() -> None:
    services = FakeServices([])
    services.queue_chars(b"\x03")
    tools, _ = _tools(services)

    with pytest.raises(keys.Aborted):
        asyncio.run(tools["read_terminal"].run({"lines": 10, "wait_seconds": 1}))


def test_read_terminal_ends_on_child_exit() -> None:
    services = FakeServices([])
    services.queue_chars(None)
    tools, _ = _tools(services)

    with pytest.raises(keys.SessionClosed):
        asyncio.run(tools["read_terminal"].run({"lines": 10, "wait_seconds": 1}))


def test_keys_typed_while_reading_are_kept_for_the_next_prompt(monkeypatch) -> None:
    """读终端时敲下的字不算丢：它属于用户，等下一次读输入再交出去。"""
    monkeypatch.setattr(agent_cmd, "READ_POLL", 0.001)
    monkeypatch.setattr(agent_cmd, "READ_QUIET", 0.005)
    services = FakeServices([], screen=FakeScreen(texts=["a", "a", "a", "a"]))
    services.queue_chars(b"x")
    tools, _ = _tools(services)

    asyncio.run(tools["read_terminal"].run({"lines": 10, "wait_seconds": 1}))

    assert services.held == [b"x"]


# ---- wait_for：把「等」变成一次调用 ----


def test_wait_for_returns_when_a_new_line_matches(monkeypatch) -> None:
    monkeypatch.setattr(agent_cmd, "READ_POLL", 0.001)
    screen = FakeScreen(texts=["a\nb", "a\nb", "a\nb\ndone"])
    tools, _ = _tools(FakeServices([], screen=screen))

    result = asyncio.run(
        tools["wait_for"].run({"pattern": "done", "timeout": 1, "lines": 10})
    )

    assert "<status>matched</status>" in result
    assert "done" in result


def test_wait_for_ignores_a_match_that_was_already_on_screen(monkeypatch) -> None:
    """等「命令跑完」时，上一个提示符本来就在屏幕上，不能一进来就算数。"""
    monkeypatch.setattr(agent_cmd, "READ_POLL", 0.001)
    screen = FakeScreen(texts=["done\n$ ", "done\n$ "])
    tools, _ = _tools(FakeServices([], screen=screen))

    result = asyncio.run(
        tools["wait_for"].run({"pattern": "done", "timeout": 0.05, "lines": 10})
    )

    assert "<status>timeout</status>" in result


def test_wait_for_quiet_pattern_waits_for_the_screen_to_settle(monkeypatch) -> None:
    monkeypatch.setattr(agent_cmd, "READ_POLL", 0.001)
    monkeypatch.setattr(agent_cmd, "READ_QUIET", 0.005)
    screen = FakeScreen(texts=["x", "x", "x", "x"])
    tools, _ = _tools(FakeServices([], screen=screen))

    result = asyncio.run(
        tools["wait_for"].run({"pattern": "", "timeout": 1, "lines": 10})
    )

    assert "<status>quiet</status>" in result


def test_wait_for_rejects_a_bad_pattern() -> None:
    tools, _ = _tools(FakeServices([]))

    with pytest.raises(ValueError):
        asyncio.run(
            tools["wait_for"].run({"pattern": "(", "timeout": 1, "lines": 10})
        )


# ---- read_file / write_file：屏幕之外的通道 ----


def test_write_file_creates_directories_and_replaces(tmp_path) -> None:
    tools, _ = _tools(FakeServices([]))
    target = tmp_path / "a" / "b.txt"

    result = asyncio.run(
        tools["write_file"].run(
            {"path": str(target), "content": "一\n二\n", "append": False}
        )
    )

    assert target.read_text(encoding="utf-8") == "一\n二\n"
    assert "已写入" in result


def test_write_file_appends(tmp_path) -> None:
    target = tmp_path / "log.txt"
    target.write_text("a\n", encoding="utf-8")
    tools, _ = _tools(FakeServices([]))

    asyncio.run(
        tools["write_file"].run(
            {"path": str(target), "content": "b\n", "append": True}
        )
    )

    assert target.read_text(encoding="utf-8") == "a\nb\n"


def test_read_file_takes_a_line_slice(tmp_path) -> None:
    target = tmp_path / "x.txt"
    target.write_text("1\n2\n3\n4\n", encoding="utf-8")
    tools, _ = _tools(FakeServices([]))

    result = asyncio.run(
        tools["read_file"].run(
            {"path": str(target), "start_line": 2, "max_lines": 2}
        )
    )

    assert "第 2-3 行，共 4 行" in result
    assert "2\n3" in result


def test_read_file_rejects_binary_and_missing_files(tmp_path) -> None:
    binary = tmp_path / "b.bin"
    binary.write_bytes(b"a\x00b")
    tools, _ = _tools(FakeServices([]))

    with pytest.raises(ValueError):
        asyncio.run(
            tools["read_file"].run(
                {"path": str(binary), "start_line": 1, "max_lines": 10}
            )
        )
    with pytest.raises(ValueError):
        asyncio.run(
            tools["read_file"].run(
                {"path": str(tmp_path / "nope"), "start_line": 1, "max_lines": 10}
            )
        )


# ---- bash：另开一个 shell ----

posix_only = pytest.mark.skipif(os.name != "posix", reason="bash 工具依赖 POSIX")


@pytest.fixture()
def restore_shell_cwd():
    """进程共用的那个 shell：`cd` 的位置会留到下一次调用，测完要还原。"""
    shell = shell_mod.shared()
    before = shell.cwd
    yield shell
    shell.cwd = before


@posix_only
def test_bash_returns_exit_code_and_both_streams(tmp_path) -> None:
    tools, _ = _tools(FakeServices([]))

    result = asyncio.run(
        tools["bash"].run(
            {"command": "echo out; echo err >&2; exit 7", "cwd": str(tmp_path), "timeout": 10}
        )
    )

    assert "退出码 7" in result
    assert "out" in result
    assert "err" in result


@posix_only
def test_bash_keeps_the_cwd_between_calls(tmp_path, restore_shell_cwd) -> None:
    (tmp_path / "a").mkdir()
    tools, _ = _tools(FakeServices([]))

    asyncio.run(
        tools["bash"].run({"command": "cd ..", "cwd": str(tmp_path / "a"), "timeout": 10})
    )
    result = asyncio.run(tools["bash"].run({"command": "pwd", "cwd": "", "timeout": 10}))

    assert str(tmp_path) == restore_shell_cwd.cwd
    assert str(tmp_path) in result


@posix_only
def test_bash_rejects_a_missing_cwd(tmp_path) -> None:
    tools, _ = _tools(FakeServices([]))

    result = asyncio.run(
        tools["bash"].run(
            {"command": "pwd", "cwd": str(tmp_path / "nope"), "timeout": 5}
        )
    )

    assert "没有这个目录" in result


@posix_only
def test_bash_times_out_and_kills_the_group(tmp_path) -> None:
    tools, _ = _tools(FakeServices([]))

    result = asyncio.run(
        tools["bash"].run({"command": "sleep 5", "cwd": str(tmp_path), "timeout": 0.2})
    )

    assert "已中止" in result


@posix_only
def test_bash_clips_a_huge_output(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(agent_cmd, "MAX_BASH_CHARS", 300)
    tools, _ = _tools(FakeServices([]))

    result = asyncio.run(
        tools["bash"].run({"command": "seq 1 5000", "cwd": str(tmp_path), "timeout": 20})
    )

    assert "中间省略" in result
    assert len(result) < 3000


@posix_only
def test_bash_rejects_an_empty_command(tmp_path) -> None:
    tools, _ = _tools(FakeServices([]))

    with pytest.raises(ValueError):
        asyncio.run(tools["bash"].run({"command": "  ", "cwd": str(tmp_path), "timeout": 5}))


# ---- /compact：先画帧、可中断 ----


def _seeded(store, turns: int = 4, filler: str = "细" * 500):
    session = store.create("stub", "m")
    for index in range(turns):
        session.append_user("旧任务 {}".format(index))
        session.append_assistant(filler)
        session.append_turn_end("ok")
    return session


def test_slash_compact_paints_before_the_summary_arrives(tmp_path) -> None:
    """摘要是一次网络请求：先摆出这个相，别让屏幕空着等好几秒。"""
    store = SessionStore(root=tmp_path)
    session = _seeded(store)
    services = FakeServices([])
    worker = agent_cmd._Session(
        services,
        ScriptedProvider([[Chunk(ChunkKind.TEXT, "摘要"), Chunk(ChunkKind.DONE)]]),
        "m",
        session,
        budget=600,
        keeper=store,
    )

    asyncio.run(worker._slash_compact(""))

    assert services.painted[0] == "({}) compacting...".format(agent_cmd.NAME)
    assert services.messages[-1].startswith("已压缩")
    session.close()


def test_slash_compact_stops_on_ctrl_c(tmp_path) -> None:
    """压缩期间按 Ctrl-C：请求取消，历史原样保留，并说清楚。"""
    store = SessionStore(root=tmp_path)
    session = _seeded(store)
    services = FakeServices([])
    services.queue_chars(b"\x03")

    class Hanging(ScriptedProvider):
        async def chat(self, model, messages, **params):
            self.requests.append(list(messages))
            await asyncio.sleep(30)
            yield Chunk(ChunkKind.DONE)

    worker = agent_cmd._Session(
        services, Hanging([]), "m", session, budget=600, keeper=store
    )

    result = asyncio.run(worker._slash_compact(""))

    assert result is agent_cmd._CANCEL
    assert services.messages[-1].endswith("历史没有改动")
    assert not [event for event in session.events() if event.type == "compact"]
    session.close()


# ---- 压缩：显式指令不被「划不划算」否决 ----


def _summaries(count: int = 1) -> ScriptedProvider:
    return ScriptedProvider(
        [[Chunk(ChunkKind.TEXT, "摘要"), Chunk(ChunkKind.DONE)] for _ in range(count)]
    )


def _worker(services, session, store, rounds=1, budget=600):
    return agent_cmd._Session(
        services, _summaries(rounds), "m", session, budget=budget, keeper=store
    )


def test_force_compacts_though_little_would_be_dropped(tmp_path) -> None:
    """用户按了 /compact 就是要压：不到 1024 tok 的划算判断不该否决这条指令。"""
    store = SessionStore(root=tmp_path)
    session = _seeded(store, turns=5, filler="细" * 300)
    services = FakeServices([])
    worker = _worker(services, session, store)

    asyncio.run(worker._slash_compact(""))

    assert services.messages[-1].startswith("已压缩")
    assert [event for event in session.events() if event.type == "compact"]
    session.close()


def test_auto_compaction_still_leaves_a_short_history_alone(tmp_path) -> None:
    """自动压缩照旧：没到水位就别动历史。"""
    store = SessionStore(root=tmp_path)
    session = _seeded(store, turns=5, filler="细" * 300)
    services = FakeServices([])
    # 预算给足：这点历史够不着 0.85 的水位，自动压缩不该发生。
    worker = _worker(services, session, store, budget=4000)
    window = worker._prepare_window(())

    assert asyncio.run(worker._compact(window)) is False
    # 自动压缩的跳过是静默的：没人在这一刻问「为什么没压」，也就不必记原因。
    assert worker._compact_skip == ""
    assert not [event for event in session.events() if event.type == "compact"]
    session.close()


def test_a_second_compact_says_it_is_already_small(tmp_path) -> None:
    """第二次 /compact：说清是上次压缩点之后没有新的历史，别拿阈值当理由。"""
    store = SessionStore(root=tmp_path)
    session = _seeded(store, turns=3, filler="细" * 200)
    services = FakeServices([])
    worker = _worker(services, session, store)

    asyncio.run(worker._slash_compact(""))
    asyncio.run(worker._slash_compact(""))

    assert "上次压缩点之后没有新的历史" in services.messages[-1]
    session.close()

# ---- 压缩：摘要的输入与长度跟着被丢量走 ----


def test_a_trimmed_transcript_keeps_the_earliest_words() -> None:
    """只留尾巴会把最早的任务目标切掉；那段恰恰是路径和约定所在。"""
    messages = [
        agent_cmd.Message("user", "最早的任务：改 ~/x 里的解析"),
        agent_cmd.Message("assistant", "中" * 5000),
        agent_cmd.Message("user", "最后一句"),
    ]

    text = agent_cmd._transcript(messages, 600)

    assert "最早的任务" in text
    assert "最后一句" in text
    assert "省略" in text
    assert len(text) < 700


def test_a_long_history_is_summarised_in_bounded_rounds(tmp_path) -> None:
    """一次请求塞不下整段历史：分块滚动，块数有上限，剩下的明说省略。"""
    store = SessionStore(root=tmp_path)
    session = _seeded(store, turns=1)
    services = FakeServices([])
    worker = _worker(services, session, store, rounds=agent_cmd.COMPACT_MAX_ROUNDS)
    messages = [agent_cmd.Message("user", "段" * 800) for _ in range(12)]

    summary = asyncio.run(worker._summarize(messages, "", source=1000, chars=120))

    assert len(worker._provider.requests) == agent_cmd.COMPACT_MAX_ROUNDS
    assert "已省略" in summary
    session.close()


def test_the_summary_budget_follows_what_is_dropped() -> None:
    """丢得多就该多读、多写；丢得少不必喂一个十万 token 的请求。"""
    small_source, small_chars = agent_cmd._compact_limits(2_000)
    big_source, big_chars = agent_cmd._compact_limits(80_000)

    assert small_source == agent_cmd.COMPACT_SOURCE_CHARS_MIN
    assert big_source > small_source
    assert big_chars > small_chars
    assert "1234 字" in agent_cmd.COMPACT_PROMPT.format(chars=1234)


# ---- 重复调用提醒 ----


def _call(guard, name, args, result):
    """把一次工具调用送进中间件，拿回它给出的观察文本。"""

    async def inner():
        return result

    return asyncio.run(guard(name, args, inner))


def test_repeat_guard_reminds_on_the_third_identical_call() -> None:
    guard = RepeatReminder()
    args = {"text": "ls", "enter": True}

    first = _call(guard, "send_text", args, "r1")
    second = _call(guard, "send_text", args, "r2")
    third = _call(guard, "send_text", args, "r3")

    assert "提醒" not in first
    assert "提醒" not in second
    assert "提醒" in third


def test_repeat_guard_resets_for_a_new_task() -> None:
    guard = RepeatReminder()
    args = {"text": "ls"}

    _call(guard, "send_text", args, "r")
    _call(guard, "send_text", args, "r")
    guard.reset()

    assert "提醒" not in _call(guard, "send_text", args, "r")


def test_repeat_guard_resets_on_a_different_call() -> None:
    guard = RepeatReminder()

    _call(guard, "send_text", {"text": "a"}, "r")
    _call(guard, "send_text", {"text": "a"}, "r")
    _call(guard, "send_text", {"text": "b"}, "r")

    assert "提醒" not in _call(guard, "send_text", {"text": "b"}, "r")


# ---- 注册 ----


def test_agent_command_is_registered() -> None:
    spec = get_command("agent")

    assert spec is not None
    assert spec.name == "agent"
    assert "a" in spec.aliases
    assert spec.category == "ai"


def test_agent_tool_schemas_are_strict_and_required() -> None:
    services = FakeServices([])
    box = builtin_tools(services, services.wait)
    definitions = {entry.tool.name: entry.tool for entry in box}

    assert set(definitions) == {
        "bash",
        "read_terminal",
        "wait_for",
        "send_text",
        "send_keys",
        "send_raw",
        "read_file",
        "write_file",
    }
    for tool in definitions.values():
        assert tool.strict is True
        assert tool.parameters["required"]
        assert tool.parameters["additionalProperties"] is False


# ---- 进度覆盖层 ----


class _OverlayScreen:
    columns = 80
    rows = 24
    alternate = False

    def __init__(self) -> None:
        self.painted = []

    def draw_overlay(self, text, cursor=None):
        self.painted.append(text)
        return 0, len(text.split("\n"))


class _OverlayServices:
    def __init__(self) -> None:
        self.screen = _OverlayScreen()

    def draw_overlay(self, text):
        return self.screen.draw_overlay(text)


def test_progress_shows_the_call_being_built() -> None:
    """工具调用分片还在流里时就把「正在拼哪个调用」画出来，不等流结束。"""
    services = _OverlayServices()
    progress = agent_cmd._Painter(services)

    progress.on_event(
        agent_cmd.AgentEvent(
            agent_cmd.AgentEventKind.CALL_DELTA,
            step=1,
            name="send_text",
            text='{"text"',
        )
    )

    first = progress.line
    assert "send_text" in first
    assert '{"text"' in first


def test_progress_replaces_the_partial_call_with_the_finished_one() -> None:
    services = _OverlayServices()
    progress = agent_cmd._Painter(services)

    progress.on_event(
        agent_cmd.AgentEvent(
            agent_cmd.AgentEventKind.CALL_DELTA, step=1, name="send_text", text="{"
        )
    )
    progress.on_event(
        agent_cmd.AgentEvent(
            agent_cmd.AgentEventKind.CALL,
            step=1,
            name="send_text",
            text='{"text": "ls"}',
        )
    )

    assert 'send_text: {"text": "ls"}' in progress.line


# ---- /undo：撤销上一轮，把原文放回输入行 ----


def test_slash_undo_takes_back_the_last_turn_and_retypes_it(
    monkeypatch, tmp_path
) -> None:
    keeper = SessionStore(root=tmp_path)
    provider = ScriptedProvider([_text("答复一")])

    services, result = _session(
        monkeypatch, provider, ["任务一", "/undo", "/exit"], keeper=keeper
    )

    session = keeper.current()
    assert session is not None
    # 那一轮从视图里没了：覆盖层、历史、轮次都不算它。
    assert session.log() == []
    assert session.history() == []
    assert session.meta.turns == 0
    # 原文还在事件流里，只是被抹掉了。
    assert any(
        event.type == "user" and event.content == "任务一"
        for event in session.events()
    )
    assert "已撤销上一轮" in services.messages[-1]
    # 原文放回输入行，且只回填一次：紧接着的下一次提示是空的。
    assert services.values == ["", "", "任务一"]
    assert result.handler == "default"
    session.close()


def test_slash_undo_says_so_when_there_is_nothing_to_take_back(monkeypatch) -> None:
    _fresh_store(monkeypatch)

    services, _ = _session(monkeypatch, ScriptedProvider([]), ["/undo", "/exit"])

    assert "没有可以撤销的轮次" in services.messages[-1]
    assert services.values == ["", ""]


def test_undo_folds_a_multi_line_task_into_one_input_line(monkeypatch, tmp_path) -> None:
    """输入行只有一行：多行原文折成一行放回，并说明折过。"""
    keeper = SessionStore(root=tmp_path)
    provider = ScriptedProvider([_text("答复")])

    services, _ = _session(
        monkeypatch, provider, ["任务一\n第二步", "/undo", "/exit"], keeper=keeper
    )

    assert services.values[-1] == "任务一 第二步"
    assert "已折成一行" in services.messages[-1]


def test_slash_undo_falls_back_to_the_memory_log() -> None:
    """不存盘的模式没有事件流可撤：从内存那两行里取回原文。"""
    services = FakeServices([])
    worker = agent_cmd._Session(services, ScriptedProvider([]), "m", None)
    worker._log = [("任务一", "答复一", "")]

    result = asyncio.run(worker._slash_undo(""))

    assert result is agent_cmd._CANCEL
    assert worker._log == []
    assert worker._retype == "任务一"
    assert "已撤销上一轮" in services.messages[-1]


def test_slash_undo_takes_no_arguments() -> None:
    services = FakeServices([])
    worker = agent_cmd._Session(services, ScriptedProvider([]), "m", None)
    worker._log = [("任务一", "答复一", "")]

    asyncio.run(worker._slash_undo("2"))

    assert "用法：/undo" in services.messages[-1]
    assert worker._log == [("任务一", "答复一", "")]


# ---- 执行中可输入：运行态输入行 + 排队 ----


def test_running_prompt_shows_the_task_status_and_the_queue() -> None:
    """执行中那一屏：历史行、在跑的一行、状态行、排队行、输入行。"""
    services = FakeServices([])
    session = agent_cmd._Session(services, ScriptedProvider([]), "m")
    session._log = [("上一轮", "旧答复", "")]
    session._queue = [agent_cmd._parse("/status"), agent_cmd._parse("下一句")]
    session._notice = "已排队，本轮结束后执行：/status"
    painter = agent_cmd._Painter(services)
    painter.begin()

    rows = session._running_text(painter, "正在跑的任务").split("\n")

    # 输入行的提示与初始态同族，只是说清楚这一轮还在跑。
    assert rows[-1] == agent_cmd.QUEUE_PROMPT
    # 上方的历史行还在：和空闲提示长得一样，不用重新认屏幕。
    assert any("旧答复" in row for row in rows)
    # 在跑的那一行与落盘、与 /status 里的一字不差。
    assert agent_cmd.TASK_PREFIX + "正在跑的任务" in rows
    assert any(row.startswith("({}) ".format(agent_cmd.NAME)) for row in rows)
    assert any("preparing..." in row for row in rows)
    # 排队的一行一条，编号看得见；斜杠命令要说明它不会发给模型。
    notes = [row for row in rows if row.startswith(agent_cmd.NOTE_PREFIX)]
    assert any("排队 1/2: /status" in row for row in notes)
    assert any("排队 2/2: 下一句" in row for row in notes)
    assert any("已排队" in row for row in notes)
    # 命令不因忙碌换脸：执行中行首一个斜杠仍是 (agent-cmd)。
    session._draft = "/s"
    assert session._running_text(painter, "正在跑的任务").split("\n")[-1] == agent_cmd.CMD_PROMPT
    session._draft = "s"
    assert session._running_text(painter, "正在跑的任务").split("\n")[-1] == agent_cmd.QUEUE_PROMPT


def test_a_line_typed_while_running_is_queued_for_the_next_turn(monkeypatch) -> None:
    """执行中敲下并回车：进队，这一轮落盘后直接接着跑它，不丢也不抢跑。"""
    provider = ScriptedProvider([_text("答复一"), _text("答复二")])

    services, result = _session(
        monkeypatch, provider, ["任务一", "/exit"], chars=["任务二".encode()]
    )

    assert len(provider.requests) == 2
    sent = provider.requests[-1]["messages"]
    assert any("任务二" in (message.content or "") for message in sent)
    # 执行中画出去的那一屏：在跑的一行 + 运行态提示，都是输入行自己的。
    assert any("(agent) 任务一" in frame for frame in services.painted)
    assert any(agent_cmd.QUEUE_PROMPT in frame for frame in services.painted)
    # 交上去输入行就空了：收起回单行，后面画的帧又只剩那一行状态。
    frames = services.painted
    last = max(i for i, frame in enumerate(frames) if agent_cmd.QUEUE_PROMPT in frame)
    assert any(agent_cmd.QUEUE_PROMPT not in frame for frame in frames[last + 1:])


def test_the_running_screen_stays_plain_until_the_first_key(monkeypatch) -> None:
    """执行中先只有那一行状态；第一下按键之后才换成运行态输入行。"""
    provider = SlowProvider([_text("答复一"), _text("答复二")])
    services = QuietFakeServices(["任务一", "/exit"], quiet_ticks=3)
    services.queue_chars("任务二".encode())
    monkeypatch.setattr(agent_cmd, "default_catalog", lambda: FakeCatalog(provider))
    _fresh_store(monkeypatch)

    asyncio.run(agent_cmd.cmd_agent(services, None))

    frames = services.painted
    switched = next(
        index for index, frame in enumerate(frames)
        if agent_cmd.QUEUE_PROMPT in frame
    )
    # 按键之前画的是老样子：只有那一行状态，没有输入行。
    assert switched >= 1
    assert all(agent_cmd.QUEUE_PROMPT not in frame for frame in frames[:switched])
    assert any(agent_cmd.NAME in frame for frame in frames[:switched])
    # 按键才是交接点：它自己也不丢，照样排进队跑了一轮。
    assert len(provider.requests) == 2


def test_ctrl_c_while_running_keeps_the_draft_for_the_next_prompt(monkeypatch) -> None:
    """中断这一轮不吞草稿：回到提示时那句还在输入行里，改完就能发。"""
    provider = ScriptedProvider([_text("答复一")])

    services, result = _session(
        monkeypatch, provider, ["任务一", "/exit"], chars=["半句话", b"\x03"]
    )

    assert "半句话" in services.values


def test_a_slash_command_typed_while_running_is_queued_not_sent(
    monkeypatch, tmp_path
) -> None:
    """执行中也能打命令：排到这一轮后面执行，绝不混成任务发给模型。"""
    keeper = SessionStore(root=tmp_path)
    provider = ScriptedProvider([_text("答复一")])

    services, result = _session(
        monkeypatch,
        provider,
        ["任务一", "/exit"],
        chars=["/title 新标题".encode()],
        keeper=keeper,
    )

    assert len(provider.requests) == 1
    assert keeper.current().meta.title == "新标题"
    # 执行中那一行输入框出现过（提示语说明回车是排队）；交上去就收起。
    assert any(agent_cmd.QUEUE_PROMPT in frame for frame in services.painted)


def test_a_line_is_parsed_before_it_is_queued() -> None:
    """排队放的是解释好的那一条：命令、别名、路径、// 当场就分好类。"""
    parsed = agent_cmd._parse("/s")
    assert parsed.kind == "command"
    assert parsed.name == "_slash_status"
    assert parsed.shown == "/s"
    assert agent_cmd._parse("/help").name == "_slash_help"
    assert agent_cmd._parse("/usr/bin/env").kind == "task"
    assert agent_cmd._parse("//ls -l").text == "/ls -l"
    assert agent_cmd._parse("/nope").kind == "unknown"
    assert agent_cmd._parse("/exit").kind == "exit"
    assert agent_cmd._parse("   ").kind == "skip"
    assert agent_cmd._parse("跑个任务").text == "跑个任务"


def test_the_expanded_screen_shows_what_is_queued(monkeypatch) -> None:
    """排完队输入行空了会收起；再敲一个字，摊开的那一屏把队里的写出来。"""
    provider = ScriptedProvider([_text("答复一")])

    services, result = _session(
        monkeypatch, provider, ["任务一", "/exit"], chars=["下一".encode(), "再"]
    )

    frames = services.painted
    assert any("排队 1/1: 下一" in frame for frame in frames)


def test_every_command_declares_what_it_does_while_a_turn_runs() -> None:
    """执行中的处置必须一条不漏地写在 _SLASH_POLICY 里。

    漏了也不报错，只是那条命令在忙时悄悄按「排到后面」处理 —— 加一条命令
    就得想清楚它属于哪一档，所以拿测试把它钉住。
    """
    methods = set(item[2] for item in agent_cmd._SLASH_COMMANDS)
    assert methods == set(agent_cmd._SLASH_POLICY)
    assert set(agent_cmd._SLASH_POLICY.values()) <= {"now", "after", "idle", "abort"}


def test_a_command_line_says_so_in_the_prompt() -> None:
    """行首一个斜杠：提示符当场换成 (agent-cmd)，回车是执行命令。"""
    session = agent_cmd._Session(FakeServices(), None, "model")

    session._draft = ""
    assert agent_cmd.INPUT_PROMPT in session._prompt_text()
    session._draft = "/s"
    assert "(agent-cmd): " in session._prompt_text()
    # // 是转义：那一行仍旧交给模型，提示符不该换成命令的。
    session._draft = "//s"
    assert "(agent-cmd): " not in session._prompt_text()
    session._draft = "s"
    assert "(agent-cmd): " not in session._prompt_text()


def test_history_up_tells_commands_from_tasks() -> None:
    """↑ 的历史分两类：命令归命令，任务归任务；路径和 // 仍旧算任务。"""
    session = agent_cmd._Session(FakeServices(), None, "model")
    cases = {
        "/s": ("cmd", agent_cmd.CMD_PROMPT),
        "/status 现在": ("cmd", agent_cmd.CMD_PROMPT),
        "/statsu": ("cmd", agent_cmd.CMD_PROMPT),  # 敲错的命令也归命令
        "修一下 agent": ("task", agent_cmd.INPUT_PROMPT),
        "//s": ("task", agent_cmd.INPUT_PROMPT),
        "/usr/bin/env python3": ("task", agent_cmd.INPUT_PROMPT),
    }
    for text, (kind, prompt) in cases.items():
        assert agent_cmd._history_kind(text) == kind, text
        # 分类和提示符是同一套判据：提示符说这是命令，↑ 就翻命令。
        session._draft = text
        assert session._prompt_text().endswith(prompt), text


def test_the_two_histories_never_mix() -> None:
    """同一个输入行里，↑ 只翻自己那一类：命令不混进任务，反之亦然。"""
    from lls.edit import LineEditor

    editor = LineEditor(
        ["/help", "任务一", "/compact", "任务二"],
        history_kind=agent_cmd._history_kind,
    )
    editor.reset("")
    editor.history_up()
    assert editor.text == "任务二"
    editor.history_up()
    assert editor.text == "任务一"

    editor.reset("/")
    editor.history_up()
    assert editor.text == "/compact"
    editor.history_up()
    assert editor.text == "/help"
    editor.history_down()
    assert editor.text == "/compact"
    editor.history_down()
    assert editor.text == "/"


def test_a_command_typed_while_running_runs_at_once(monkeypatch) -> None:
    """执行中敲 /help：当场执行，不排到这一轮后面，也不会变成任务。"""
    provider = ScriptedProvider([_text("答复一")])

    services, result = _session(
        monkeypatch, provider, ["任务一", "/exit"], chars=[b"/help"]
    )

    assert any("会话内命令" in text for text in services.messages)
    assert not any("排队" in frame for frame in services.painted)
    assert len(provider.requests) == 1


def test_a_command_that_would_step_on_the_turn_is_refused(monkeypatch) -> None:
    """执行中 /undo：拒绝，并说清下一步是 Ctrl-C，不是默默排队。"""
    provider = ScriptedProvider([_text("答复一")])

    services, result = _session(monkeypatch, provider, ["任务一"], chars=[b"/undo"])

    assert any("先 Ctrl-C" in text for text in services.messages)
    assert len(provider.requests) == 1


def test_exit_typed_while_running_leaves_after_this_turn(monkeypatch) -> None:
    """执行中打 /exit：先让这一轮收尾（中断也只是放弃本轮），再退出。"""
    provider = ScriptedProvider([_text("答复一")])

    services, result = _session(monkeypatch, provider, ["任务一"], chars=[b"/exit"])

    assert result.handler == "default"
    assert len(provider.requests) == 1


def test_the_child_going_away_during_a_turn_ends_the_turn_cleanly(
    monkeypatch, tmp_path
) -> None:
    """执行中子进程没了：这一轮照样写 turn_end，不是被我们撕掉的。"""
    keeper = SessionStore(root=tmp_path)
    provider = ScriptedProvider([_text("答复一")])

    services, result = _session(
        monkeypatch, provider, ["任务一"], chars=[None], keeper=keeper
    )

    session = keeper.current()
    statuses = [event.status for event in session.events() if event.type == "turn_end"]
    assert statuses == ["aborted"]
    session.close()


def test_the_thin_service_lists_every_keyword_the_engine_takes() -> None:
    """服务层是引擎的窄口子：引擎收的关键字这里得一个不少。

    测试里的假服务用 ``**kwargs`` 照单全收，所以少列一个只在真终端里炸
    （agent 传 ``history_kind``，服务层没这一项 → 一按回车就 TypeError）。
    """
    from lls.session.engine import Engine
    from lls.session.services import Services

    engine = set(inspect.signature(Engine.read_line).parameters) - {"self"}
    thin = set(inspect.signature(Services.read_line).parameters) - {"self"}
    assert engine == thin
