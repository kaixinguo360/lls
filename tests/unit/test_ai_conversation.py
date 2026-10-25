"""多轮对话的单元测试。

协议要求：assistant 的 ``tool_calls`` 后面必须紧跟对应的 ``tool`` 回复。
因此历史按「轮」存储 —— 一轮要么完整，要么完全不进。
"""

from __future__ import annotations

from lls.ai import Conversation, Message, ToolCall

USER = Message("user", "<terminal_state/>\n<request>ls</request>")


def _call(call_id: str = "call_1") -> ToolCall:
    return ToolCall(id=call_id, name="emit_input", arguments='{"input": "ls"}')


def test_empty_conversation() -> None:
    conversation = Conversation()

    assert len(conversation) == 0
    assert conversation.messages() == []


def test_turn_records_user_assistant_and_tool() -> None:
    conversation = Conversation()

    conversation.add_turn(USER, _call(), "written")

    assert [m.role for m in conversation.messages()] == ["user", "assistant", "tool"]


def test_assistant_message_carries_the_call() -> None:
    conversation = Conversation()
    call = _call()

    conversation.add_turn(USER, call, "written")
    assistant = conversation.messages()[1]

    assert assistant.content is None
    assert assistant.tool_calls == (call,)


def test_tool_message_answers_the_call() -> None:
    conversation = Conversation()
    call = _call("call_42")

    conversation.add_turn(USER, call, "written")
    tool = conversation.messages()[2]

    assert tool.role == "tool"
    assert tool.tool_call_id == "call_42"
    assert tool.content == "written"


def test_turn_without_a_model_call_keeps_only_the_user_message() -> None:
    """``请求#内容`` 这种没经过模型的轮次，没有需要回应的工具调用。"""
    conversation = Conversation()

    conversation.add_turn(USER, None, "written")

    assert [m.role for m in conversation.messages()] == ["user"]


def test_turns_accumulate_in_order() -> None:
    conversation = Conversation()

    conversation.add_turn(Message("user", "one"), _call("a"), "written")
    conversation.add_turn(Message("user", "two"), _call("b"), "written")

    contents = [m.content for m in conversation.messages()]
    assert contents[0] == "one"
    assert contents[3] == "two"


def test_history_is_trimmed_by_whole_turns() -> None:
    conversation = Conversation(max_turns=2)

    for index in range(5):
        conversation.add_turn(Message("user", f"turn{index}"), _call(), "written")

    assert len(conversation) == 2
    # 保留的是最后两轮，且结构完整
    assert [m.role for m in conversation.messages()] == [
        "user",
        "assistant",
        "tool",
        "user",
        "assistant",
        "tool",
    ]
    assert conversation.messages()[0].content == "turn3"
    assert conversation.messages()[3].content == "turn4"


def test_max_turns_is_at_least_one() -> None:
    conversation = Conversation(max_turns=0)

    conversation.add_turn(Message("user", "only"), _call(), "written")

    assert len(conversation) == 1


def test_clear_empties_the_conversation() -> None:
    conversation = Conversation()
    conversation.add_turn(USER, _call(), "written")

    conversation.clear()

    assert conversation.messages() == []
