"""交互式 agent 会话：每轮任务自动执行，结束后回到同一会话提示。

覆盖会话状态机（提示、历史、日志、退出）、工具行为、取消与失败路径，
以及不限步数的长任务。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from lls.ai import Chunk, ChunkKind, ConfigError, ToolCall
from lls.commands import get_command
from lls.commands.builtin import agent as agent_cmd
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
            return "line-%d" % self._calls
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
        self._chars = []

    def queue_chars(self, *values) -> None:
        self._chars.extend(values)

    async def read_line(self, prompt, **kwargs):
        self.prompts.append(prompt)
        return self._answers.pop(0) if self._answers else None

    async def read_chars(self, timeout=None):
        # 让出事件循环：真实实现会等输入，模型流任务才有机会推进。
        await asyncio.sleep(0)
        if self._chars:
            return self._chars.pop(0)
        return b""

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


class FakeCatalog:
    def __init__(self, provider) -> None:
        self._provider = provider

    def resolve(self):
        return self._provider, "stub-model"


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
    return [Chunk(ChunkKind.TOOL_CALL, call=call) for call in calls] + [
        Chunk(ChunkKind.DONE)
    ]


def _call(call_id: str, name: str, arguments) -> ToolCall:
    raw = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return ToolCall(id=call_id, name=name, arguments=raw)


def _session(monkeypatch, provider, answers, *, screen=None, first_task=None, chars=()):
    services = FakeServices(answers, screen=screen)
    if chars:
        services.queue_chars(*chars)
    monkeypatch.setattr(agent_cmd, "default_catalog", lambda: FakeCatalog(provider))
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
    # 被中断的一轮不进入会话历史
    assert all(
        "任务1" not in (message.content or "")
        for message in provider.requests[-1]["messages"]
    )


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
                "c%d" % index,
                "send_text",
                {"text": "step-%d" % index, "enter": False},
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


def test_history_is_trimmed_to_the_configured_turns(monkeypatch) -> None:
    monkeypatch.setattr(agent_cmd, "HISTORY_TURNS", 2)
    provider = ScriptedProvider([_text("a1"), _text("a2"), _text("a3")])

    services, result = _session(
        monkeypatch, provider, ["任务1", "任务2", "任务3", "/exit"]
    )

    assert result.handler == "default"
    for request in provider.requests:
        users = [m for m in request["messages"] if m.role == "user"]
        assert len(users) <= 2
    assert all(
        "任务1" not in (message.content or "")
        for message in provider.requests[2]["messages"]
        if message.role == "user"
    )


# ---- 提示构造 ----


def test_prompt_shows_the_last_reply_and_stays_within_bounds(monkeypatch) -> None:
    services = FakeServices([])
    session = agent_cmd._Session(services, ScriptedProvider([]), "m")
    session._log = [("任务" * 40, "答复" * 60, "备注" * 20), ("任务二", "答复二", "")]

    prompt = session._prompt_text()

    assert "答复" in prompt
    rows = prompt.split("\n")
    assert len(rows) <= agent_cmd.LOG_LINES + 2
    for row in rows:
        assert agent_cmd._width(row) <= services.screen.columns


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
    assert len(rows) <= agent_cmd.REPLY_LINES


# ---- 思考预览：始终显示最新一段 ----


def test_tail_line_keeps_the_end_of_a_long_text() -> None:
    line = agent_cmd._tail_line("(t) ", "0123456789", 10)

    assert line.startswith("(t) …")
    assert line.endswith("6789")
    assert agent_cmd._width(line) <= 10


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
    painter._paint(force=True)

    shown = services.painted[-1]
    assert "END" in shown
    assert "START" not in shown


# ---- 工具 ----


def _tools(services):
    view = agent_cmd._TerminalView(services)
    guard = agent_cmd._RepeatGuard()
    tools = {entry.tool.name: entry for entry in agent_cmd._build_tools(services, view, guard)}
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

    with pytest.raises(agent_cmd._Aborted):
        asyncio.run(tools["read_terminal"].run({"lines": 10, "wait_seconds": 1}))


def test_read_terminal_ends_on_child_exit() -> None:
    services = FakeServices([])
    services.queue_chars(None)
    tools, _ = _tools(services)

    with pytest.raises(agent_cmd._SessionEnded):
        asyncio.run(tools["read_terminal"].run({"lines": 10, "wait_seconds": 1}))


def test_read_forwards_terminal_responses_but_drops_user_keys(monkeypatch) -> None:
    monkeypatch.setattr(agent_cmd, "READ_POLL", 0.001)
    monkeypatch.setattr(agent_cmd, "READ_QUIET", 0.005)
    services = FakeServices([], screen=FakeScreen(texts=["a", "a", "a", "a"]))
    # 用户敲的字符属于会话期间的输入，丢弃；终端对查询的应答必须转交。
    services.queue_chars(b"x", b"\x1b[12;40R")
    tools, _ = _tools(services)

    asyncio.run(tools["read_terminal"].run({"lines": 10, "wait_seconds": 1}))

    assert services.written == [(b"\x1b[12;40R", False)]


# ---- 重复调用提醒 ----


def test_repeat_guard_reminds_on_the_third_identical_call() -> None:
    guard = agent_cmd._RepeatGuard()
    args = {"text": "ls", "enter": True}

    first = guard.after("send_text", args, "r1")
    second = guard.after("send_text", args, "r2")
    third = guard.after("send_text", args, "r3")

    assert "提醒" not in first
    assert "提醒" not in second
    assert "提醒" in third


def test_repeat_guard_resets_for_a_new_task() -> None:
    guard = agent_cmd._RepeatGuard()
    args = {"text": "ls"}

    guard.after("send_text", args, "r")
    guard.after("send_text", args, "r")
    guard.reset()

    assert "提醒" not in guard.after("send_text", args, "r")


def test_repeat_guard_resets_on_a_different_call() -> None:
    guard = agent_cmd._RepeatGuard()

    guard.after("send_text", {"text": "a"}, "r")
    guard.after("send_text", {"text": "a"}, "r")
    guard.after("send_text", {"text": "b"}, "r")

    assert "提醒" not in guard.after("send_text", {"text": "b"}, "r")


# ---- 注册 ----


def test_agent_command_is_registered() -> None:
    spec = get_command("agent")

    assert spec is not None
    assert spec.name == "agent"
    assert "a" in spec.aliases
    assert spec.category == "ai"


def test_agent_tool_schemas_are_strict_and_required() -> None:
    services = FakeServices([])
    view = agent_cmd._TerminalView(services)
    definitions = {
        entry.tool.name: entry.tool
        for entry in agent_cmd._build_tools(services, view, agent_cmd._RepeatGuard())
    }

    assert set(definitions) == {"read_terminal", "send_text", "send_keys", "send_raw"}
    for tool in definitions.values():
        assert tool.strict is True
        assert tool.parameters["required"]
        assert tool.parameters["additionalProperties"] is False
