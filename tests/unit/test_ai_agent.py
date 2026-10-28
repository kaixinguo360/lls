"""通用 Agent 循环：继续/结束只看本轮有没有工具调用。

与 DSH / opencode 一致：没有工具调用 = 正常结束，assistant 文本即最终答复；
不设 finish 工具。默认不限步数；调用方给上限时，最后一轮禁用工具、只让模型
总结（见末尾几组用例）。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from lls.ai import (
    Agent,
    AgentCancelled,
    AgentEventKind,
    AgentTool,
    Chunk,
    ChunkKind,
    Message,
    Tool,
    ToolCall,
)

SYSTEM = "system prompt"


# ---- 桩 ----


class ScriptedProvider:
    """按脚本逐轮产出 chunk，并记录每次请求。"""

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
                "parallel_tool_calls": parallel_tool_calls,
            }
        )
        for chunk in self.rounds.pop(0):
            yield chunk


def _text(value: str):
    return [Chunk(ChunkKind.TEXT, value), Chunk(ChunkKind.DONE)]


def _calls(*calls) -> list:
    return [Chunk(ChunkKind.TOOL_CALL, call=call) for call in calls] + [
        Chunk(ChunkKind.DONE)
    ]


def _call(call_id: str, name: str, arguments) -> ToolCall:
    raw = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return ToolCall(id=call_id, name=name, arguments=raw)


def _tool(name: str, handler) -> AgentTool:
    return AgentTool(Tool(name=name, parameters={"type": "object"}), handler)


def _run(
    provider,
    task: str = "任务",
    *,
    tools=(),
    history=(),
    max_steps: int | None = None,
    events=None,
    poll=None,
) -> object:
    agent = Agent(
        provider,
        "stub-model",
        system=SYSTEM,
        tools=list(tools),
        max_steps=max_steps,
        step_limit_note="总结吧",
        on_event=(events.append if events is not None else None),
        poll=poll,
    )
    return asyncio.run(agent.run(task, history))


# ---- 继续 / 结束 ----


def test_plain_text_ends_the_run() -> None:
    provider = ScriptedProvider([_text("完成")])

    result = _run(provider, "任务")

    assert result.reason == "completed"
    assert result.summary == "完成"
    assert [message.role for message in result.messages] == ["user", "assistant"]
    assert result.messages[0].content == "任务"
    assert result.messages[0].role == "user"


def test_system_prompt_is_sent_but_not_returned() -> None:
    provider = ScriptedProvider([_text("ok")])

    result = _run(provider, "任务")

    assert provider.requests[0]["messages"][0] == Message("system", SYSTEM)
    assert all(message.role != "system" for message in result.messages)


def test_empty_reply_without_tools_is_still_a_normal_end() -> None:
    result = _run(ScriptedProvider([[]]), "任务")

    assert result.reason == "completed"
    assert result.summary == ""
    assert result.messages[-1].role == "assistant"
    assert result.messages[-1].content == ""


def test_tool_call_is_executed_and_answered() -> None:
    seen = []

    async def handler(args):
        seen.append(args)
        return "done"

    provider = ScriptedProvider(
        [_calls(_call("c1", "send_text", {"text": "ls"})), _text("ok")]
    )

    result = _run(provider, "任务", tools=[_tool("send_text", handler)])

    assert seen == [{"text": "ls"}]
    assert [message.role for message in result.messages] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert result.messages[1].tool_calls[0].id == "c1"
    assert result.messages[2].tool_call_id == "c1"
    assert result.messages[2].content == "done"
    assert provider.requests[1]["messages"][-1] == Message(
        "tool", "done", tool_call_id="c1"
    )


def test_multiple_calls_in_one_round_run_in_order() -> None:
    order = []

    async def first(args):
        order.append("first")
        return "1"

    async def second(args):
        order.append("second")
        return "2"

    provider = ScriptedProvider(
        [
            _calls(_call("a", "first", {}), _call("b", "second", {})),
            _text("done"),
        ]
    )

    result = _run(
        provider, "任务", tools=[_tool("first", first), _tool("second", second)]
    )

    assert order == ["first", "second"]
    assert [message.role for message in result.messages] == [
        "user",
        "assistant",
        "tool",
        "tool",
        "assistant",
    ]
    assert [
        message.tool_call_id for message in result.messages if message.role == "tool"
    ] == ["a", "b"]


# ---- 工具错误：转成观察，循环继续 ----


def test_unknown_tool_becomes_an_error_result() -> None:
    provider = ScriptedProvider([_calls(_call("x", "nope", {})), _text("ok")])

    result = _run(provider, "任务", tools=[])

    content = result.messages[2].content
    assert content.startswith("error: unknown tool")
    assert "nope" in content
    assert result.reason == "completed"
    assert len(provider.requests) == 2


def test_invalid_json_is_reported_without_calling_the_tool() -> None:
    called = []

    async def handler(args):
        called.append(args)
        return "x"

    provider = ScriptedProvider(
        [_calls(_call("x", "send_text", "{oops")), _text("ok")]
    )

    result = _run(provider, "任务", tools=[_tool("send_text", handler)])

    assert called == []
    assert result.messages[2].content.startswith("error: invalid tool arguments")


def test_non_object_arguments_are_rejected() -> None:
    provider = ScriptedProvider([_calls(_call("x", "send_text", "[]")), _text("ok")])

    result = _run(provider, "任务", tools=[_tool("send_text", _noop)])

    assert result.messages[2].content == "error: tool arguments must be a JSON object"


async def _noop(args):
    return "ok"


def test_tool_exception_becomes_an_error_result_and_the_loop_continues() -> None:
    async def boom(args):
        raise RuntimeError("kaput")

    provider = ScriptedProvider([_calls(_call("x", "send_text", {})), _text("ok")])

    result = _run(provider, "任务", tools=[_tool("send_text", boom)])

    assert result.messages[2].content == "error: kaput"
    assert result.reason == "completed"
    assert len(provider.requests) == 2


def test_finish_named_tool_has_no_special_meaning() -> None:
    """没有 finish 语义：普通工具，调用后照常进入下一轮。"""
    seen = []

    async def handler(args):
        seen.append(args)
        return "done"

    provider = ScriptedProvider(
        [_calls(_call("f", "finish_task", {"summary": "x"})), _text("继续了")]
    )

    result = _run(provider, "任务", tools=[_tool("finish_task", handler)])

    assert seen == [{"summary": "x"}]
    assert result.reason == "completed"
    assert result.summary == "继续了"
    assert len(provider.requests) == 2


# ---- 可选步数上限 ----


def test_step_limit_disables_tools_and_hides_the_nudge() -> None:
    seen = []

    async def handler(args):
        seen.append(args)
        return "done"

    provider = ScriptedProvider(
        [
            _calls(_call("c1", "send_text", {"text": "ls"})),
            _calls(_call("c2", "send_text", {"text": "ls -l"})),
            _text("总结"),
        ]
    )

    result = _run(
        provider, "任务", tools=[_tool("send_text", handler)], max_steps=3
    )

    assert result.reason == "step_limit"
    assert result.summary == "总结"
    assert result.steps == 3
    assert provider.requests[2]["tools"] is None
    assert provider.requests[2]["parallel_tool_calls"] is None
    nudge = provider.requests[2]["messages"][-1]
    assert nudge.role == "user"
    assert nudge.content.startswith("<step-limit>")
    assert all("<step-limit>" not in (message.content or "") for message in result.messages)
    assert [message.role for message in result.messages] == [
        "user",
        "assistant",
        "tool",
        "assistant",
        "tool",
        "assistant",
    ]
    assert seen == [{"text": "ls"}, {"text": "ls -l"}]


def test_max_steps_is_at_least_two() -> None:
    async def handler(args):
        return "done"

    provider = ScriptedProvider([_calls(_call("c", "t", {})), _text("总结")])

    result = _run(
        provider, "任务", tools=[_tool("t", handler)], max_steps=1
    )

    assert result.reason == "step_limit"
    assert provider.requests[1]["tools"] is None


def test_unlimited_steps_keep_going_until_the_model_stops() -> None:
    """默认不限步数：十几轮工具调用也照常继续，不会出现收尾轮。"""
    provider = ScriptedProvider(
        [_calls(_call(f"c{index}", "t", {})) for index in range(12)]
        + [_text("done")]
    )

    result = _run(provider, "任务", tools=[_tool("t", _noop)])

    assert result.reason == "completed"
    assert result.summary == "done"
    assert result.steps == 13
    assert all(request["tools"] is not None for request in provider.requests)
    assert all(
        "<step-limit>" not in (message.content or "")
        for message in result.messages
    )


# ---- 取消 ----


def test_poll_cancels_the_stream_and_closes_it() -> None:
    state = {"closed": False}

    class SlowProvider:
        id = "slow"

        async def chat(self, *args, **kwargs):
            try:
                yield Chunk(ChunkKind.TEXT, "a")
                await asyncio.sleep(10)
            finally:
                state["closed"] = True

    async def poll(timeout):
        # 先让流任务跑起来，再模拟用户中断：这样取消会传进生成器的 finally。
        await asyncio.sleep(0)
        raise AgentCancelled("stop")

    agent = Agent(
        SlowProvider(),
        "m",
        system=SYSTEM,
        tools=[],
        max_steps=4,
        step_limit_note="n",
        poll=poll,
    )

    with pytest.raises(AgentCancelled):
        asyncio.run(agent.run("任务"))

    assert state["closed"] is True


# ---- 事件与请求形状 ----


def test_events_cover_stream_and_tools() -> None:
    events = []

    async def handler(args):
        return "ok"

    provider = ScriptedProvider(
        [
            [Chunk(ChunkKind.REASONING, "想"), *_calls(_call("c", "t", {}))],
            _text("done"),
        ]
    )

    _run(provider, "任务", tools=[_tool("t", handler)], events=events)

    kinds = [event.kind for event in events]
    for expected in (
        AgentEventKind.STEP,
        AgentEventKind.REASONING,
        AgentEventKind.CALL,
        AgentEventKind.RESULT,
        AgentEventKind.TEXT,
    ):
        assert expected in kinds


def test_tools_and_parallel_flag_are_sent() -> None:
    definition = Tool(name="t", description="d", parameters={"type": "object"})

    async def handler(args):
        return "ok"

    provider = ScriptedProvider([_calls(_call("c", "t", {})), _text("done")])

    _run(provider, "任务", tools=[AgentTool(definition, handler)])

    request = provider.requests[0]
    assert request["tools"] == [definition]
    assert request["parallel_tool_calls"] is False


def test_history_is_inserted_after_system() -> None:
    history = [Message("user", "旧任务"), Message("assistant", "旧答复")]
    provider = ScriptedProvider([_text("新答复")])

    result = _run(provider, "新任务", history=history)

    roles = [message.role for message in provider.requests[0]["messages"]]
    assert roles == ["system", "user", "assistant", "user"]
    assert provider.requests[0]["messages"][1:] == history + [
        Message("user", "新任务")
    ]
    assert [message.role for message in result.messages] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]


# ---- 增量回调与用量 ----


def test_each_message_reaches_on_message_with_its_usage() -> None:
    seen = []
    provider = ScriptedProvider(
        [
            [
                Chunk(ChunkKind.TEXT, "先看看"),
                Chunk(ChunkKind.TOOL_CALL, call=ToolCall("c1", "shell", "{}")),
                Chunk(ChunkKind.USAGE, usage={"prompt": 10, "completion": 2}),
                Chunk(ChunkKind.DONE),
            ],
            [Chunk(ChunkKind.TEXT, "好了"), Chunk(ChunkKind.DONE)],
        ]
    )
    agent = Agent(
        provider,
        "m",
        system=SYSTEM,
        tools=[_tool("shell", _noop)],
        on_message=lambda message, usage: seen.append((message.role, usage)),
    )

    asyncio.run(agent.run("任务"))

    assert [role for role, _ in seen] == ["user", "assistant", "tool", "assistant"]
    assert seen[0][1] is None
    assert seen[1][1] == {"prompt": 10, "completion": 2}
    assert seen[3][1] is None


def test_result_carries_the_last_usage() -> None:
    provider = ScriptedProvider(
        [
            [
                Chunk(ChunkKind.TEXT, "答复"),
                Chunk(ChunkKind.USAGE, usage={"prompt": 7, "completion": 3}),
                Chunk(ChunkKind.DONE),
            ]
        ]
    )
    agent = Agent(provider, "m", system=SYSTEM, tools=[])

    result = asyncio.run(agent.run("任务"))

    assert result.usage == {"prompt": 7, "completion": 3}
