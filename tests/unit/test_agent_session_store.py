"""会话存储：事件流是事实来源，窗口是派生视图。

这里只测存储本身（目录布局、追加、折叠、blobs、锁、归档、分叉）；
命令层是否把状态接上在 ``test_agent_session.py``。
"""

from __future__ import annotations

import importlib
import json
import re
from pathlib import Path

import pytest

from lls.ai import Message, ToolCall
from lls.ai.sessions import (
    SessionBusy,
    SessionError,
    SessionStore,
    estimate_tokens,
    fit,
    fold,
    last_turns,
    task_message,
)
from lls.ai.sessions.events import (
    INTERRUPT_MARKER,
    MISSING_BLOB,
    SUMMARY_PREFIX,
    Event,
)

store_module = importlib.import_module("lls.ai.sessions.store")

# ---- 建会话与目录布局 ----


def test_a_new_session_lives_in_its_own_directory(tmp_path: Path) -> None:
    keeper = SessionStore(root=tmp_path)

    session = keeper.create("stub", "stub-model")

    assert re.match(r"^\d{8}-\d{4}-[0-9a-f]{6}$", session.id)
    assert (tmp_path / session.id / "meta.json").is_file()
    assert (tmp_path / session.id / "events.jsonl").is_file()
    assert keeper.current_id() == session.id
    session.close()

    raw = json.loads((tmp_path / session.id / "meta.json").read_text("utf-8"))
    assert raw["provider_id"] == "stub"
    assert raw["model_id"] == "stub-model"


def test_session_start_records_model_and_cwd(tmp_path: Path) -> None:
    keeper = SessionStore(root=tmp_path)
    session = keeper.create("stub", "m")

    first = session.events()[0]

    assert first.type == "session_start"
    assert first.model == "stub/m"
    assert first.note  # cwd
    session.close()


def test_two_sessions_get_different_ids(tmp_path: Path) -> None:
    keeper = SessionStore(root=tmp_path)

    first = keeper.create("stub", "m")
    second = keeper.create("stub", "m")

    assert first.id != second.id
    assert keeper.current_id() == second.id
    first.close()
    second.close()


# ---- 追加与读回 ----


def test_appended_events_come_back_in_order(tmp_path: Path) -> None:
    keeper = SessionStore(root=tmp_path)
    session = keeper.create("stub", "m")

    session.append_user("任务")
    session.append_assistant("答复")

    events = session.events()
    assert [event.seq for event in events] == [1, 2, 3]
    assert [event.type for event in events] == ["session_start", "user", "assistant"]
    assert events[1].content == "任务"
    assert events[2].content == "答复"
    session.close()


def test_meta_tracks_seq_size_title_and_turns(tmp_path: Path) -> None:
    keeper = SessionStore(root=tmp_path)
    session = keeper.create("stub", "m")

    session.append_user("把  很长的   任务写成一行标题" + "啊" * 60)
    session.append_assistant("答复")
    session.append_turn_end("ok")

    assert session.meta.title.endswith("啊")
    assert len(session.meta.title) == 40
    assert session.meta.turns == 1
    assert session.meta.last_seq == 4
    assert session.meta.size > 0
    session.close()


def test_a_truncated_last_line_only_loses_that_event(tmp_path: Path) -> None:
    keeper = SessionStore(root=tmp_path)
    session = keeper.create("stub", "m")
    session.append_user("完整的一条")
    path = session.path / "events.jsonl"
    session.close()

    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"type":"assistant","seq":3,"ts":1.0,"cont')

    reopened = keeper.open()
    assert reopened is not None
    assert [event.type for event in reopened.events()] == ["session_start", "user"]
    assert reopened.bad_lines == 1
    assert reopened.history() == [Message("user", "完整的一条")]
    reopened.close()


# ---- 折叠 ----


def test_fold_builds_messages_and_keeps_tool_pairs(tmp_path: Path) -> None:
    keeper = SessionStore(root=tmp_path)
    session = keeper.create("stub", "m")
    session.append_user("任务")
    session.append_assistant("", (ToolCall("c1", "shell", "{}"),))
    session.append_tool("c1", "输出")
    session.append_assistant("答复")

    assert session.history() == [
        Message("user", "任务"),
        Message("assistant", None, tool_calls=(ToolCall("c1", "shell", "{}"),)),
        Message("tool", "输出", tool_call_id="c1"),
        Message("assistant", "答复"),
    ]
    session.close()


def test_fold_answers_a_tool_call_that_was_never_answered() -> None:
    """中断、崩溃都会留下没有回复的调用；补一条，别让请求缺配对。"""
    events = [
        Event(type="user", seq=1, content="任务"),
        Event(
            type="assistant",
            seq=2,
            tool_calls=(ToolCall("c1", "shell", "{}"),),
        ),
    ]

    messages = fold(events)
    assert [message.role for message in messages] == ["user", "assistant", "tool"]
    assert messages[-1].tool_call_id == "c1"
    assert messages[-1].content.startswith("error:")


def test_fold_keeps_an_aborted_turn_and_marks_it(tmp_path: Path) -> None:
    """被中断的一轮留在上下文里：模型得知道上一轮停在哪儿、没做完。"""
    keeper = SessionStore(root=tmp_path)
    session = keeper.create("stub", "m")
    session.append_user("任务")
    session.append_assistant("", (ToolCall("c1", "shell", "{}"),))
    session.append_turn_end("aborted", "已中断")

    messages = session.history()
    assert [message.role for message in messages] == ["user", "assistant", "tool", "user"]
    assert messages[2].tool_call_id == "c1"
    assert messages[2].content == "error: 这次调用被用户中断，没有结果"
    assert messages[3].content == INTERRUPT_MARKER
    session.close()


def test_fold_keeps_a_failed_turn_without_a_marker(tmp_path: Path) -> None:
    """出错的一轮也留下：答复可能没写出来，但用户看见的任务不该消失。"""
    keeper = SessionStore(root=tmp_path)
    session = keeper.create("stub", "m")
    session.append_user("任务")
    session.append_turn_end("error", "error: boom")

    assert session.history() == [Message("user", "任务")]
    session.close()


def test_log_entries_come_from_user_and_turn_end(tmp_path: Path) -> None:
    keeper = SessionStore(root=tmp_path)
    session = keeper.create("stub", "m")
    session.append_user("任务一")
    session.append_assistant("答复一")
    session.append_turn_end("ok")
    session.append_user("任务二")
    session.append_assistant("答复二")
    session.append_turn_end("aborted", "已中断")

    assert session.log(2) == [("任务一", "答复一", ""), ("任务二", "答复二", "已中断")]
    session.close()


def test_compact_replaces_the_covered_range(tmp_path: Path) -> None:
    keeper = SessionStore(root=tmp_path)
    session = keeper.create("stub", "m")
    session.append_user("旧任务")
    session.append_assistant("旧答复")
    upto = session.meta.last_seq
    session.append_user("新任务")
    session.append_compact("之前在做旧任务，结论是 X", upto)

    history = session.history()
    assert history[0].content == SUMMARY_PREFIX + "之前在做旧任务，结论是 X"
    assert [message.role for message in history] == ["user", "user"]
    assert history[-1].content == "新任务"
    session.close()


# ---- 随任务发出去的画面 ----


def test_a_turn_replays_the_screen_it_was_sent_with(tmp_path: Path) -> None:
    keeper = SessionStore(root=tmp_path)
    session = keeper.create("stub", "m")
    screen = "$ pytest -q\n3 passed"

    snapshot = session.append_screen(screen)
    session.append_user("跑一下测试", screen=snapshot.seq)

    assert session.history() == [Message("user", task_message("跑一下测试", screen))]
    # 覆盖层要的是任务原文，不是拼好的那条消息。
    session.append_turn_end("ok")
    assert session.log(1) == [("跑一下测试", "", "")]
    session.close()


def test_a_turn_without_its_screen_falls_back_to_the_task(tmp_path: Path) -> None:
    keeper = SessionStore(root=tmp_path)
    session = keeper.create("stub", "m")

    session.append_user("任务", screen=999)

    assert session.history() == [Message("user", "任务")]
    session.close()


def test_a_big_screen_goes_to_blobs(tmp_path: Path) -> None:
    keeper = SessionStore(root=tmp_path)
    session = keeper.create("stub", "m")
    screen = "行\n" * 5000

    snapshot = session.append_screen(screen)
    session.append_user("任务", screen=snapshot.seq)

    assert snapshot.blob and snapshot.size > store_module.BLOB_BYTES
    assert (session.path / "blobs" / snapshot.blob).is_file()
    assert session.history()[-1] == Message("user", task_message("任务", screen))
    assert "行\n行\n行" not in (session.path / "events.jsonl").read_text("utf-8")
    session.close()


def test_a_screen_whose_blob_is_gone_falls_back_to_the_task(tmp_path: Path) -> None:
    keeper = SessionStore(root=tmp_path)
    session = keeper.create("stub", "m")
    snapshot = session.append_screen("行\n" * 5000)
    session.append_user("任务", screen=snapshot.seq)
    (session.path / "blobs" / snapshot.blob).unlink()

    assert session.history() == [Message("user", "任务")]
    session.close()


# ---- 只读尾部 ----


def test_the_context_starts_after_the_last_compaction(tmp_path: Path) -> None:
    keeper = SessionStore(root=tmp_path)
    session = keeper.create("stub", "m")
    session.append_user("旧任务")
    session.append_assistant("旧答复")
    upto = session.meta.last_seq
    session.append_compact("旧任务已经做完，结论是 X", upto)
    session.append_user("新任务")

    recent = session.recent()

    assert recent[0].type == "compact"
    assert [event.content for event in recent[1:]] == ["新任务"]
    assert session.history()[0].content == SUMMARY_PREFIX + "旧任务已经做完，结论是 X"
    assert session.compacted_upto() == upto
    session.close()


def test_looking_back_from_the_end_does_not_return_the_first_event(
    tmp_path: Path,
) -> None:
    keeper = SessionStore(root=tmp_path)
    session = keeper.create("stub", "m")

    # 尾部倒读找不到目标时会把整份日志读完，第一条是 session_start。
    assert session.last_turn_end() is None
    assert session.usage() is None
    assert session.compacted_upto() == 0
    session.close()


# ---- 大块输出落 blobs ----


def test_a_big_tool_output_goes_to_blobs(tmp_path: Path) -> None:
    keeper = SessionStore(root=tmp_path)
    session = keeper.create("stub", "m")
    text = "行\n" * 5000

    session.append_assistant("", (ToolCall("c1", "shell", "{}"),))
    session.append_tool("c1", text)

    event = session.events()[-1]
    assert event.blob and event.size > 8192
    assert event.content == ""
    assert (session.path / "blobs" / event.blob).is_file()
    assert session.history()[-1] == Message("tool", text, tool_call_id="c1")
    session.close()


def test_a_missing_blob_leaves_a_placeholder_not_a_gap(tmp_path: Path) -> None:
    keeper = SessionStore(root=tmp_path)
    session = keeper.create("stub", "m")
    session.append_user("任务")
    session.append_assistant("", (ToolCall("c1", "shell", "{}"),))
    session.append_tool("c1", "大" * 5000)
    event = session.events()[-1]
    (session.path / "blobs" / event.blob).unlink()

    history = session.history()

    assert history[-1] == Message("tool", MISSING_BLOB, tool_call_id="c1")
    session.close()


def test_events_stay_small_when_the_output_is_big(tmp_path: Path) -> None:
    keeper = SessionStore(root=tmp_path)
    session = keeper.create("stub", "m")
    session.append_tool("c1", "大" * 5000)
    session.close()

    raw = (session.path / "events.jsonl").read_text("utf-8")
    assert "大" * 100 not in raw


# ---- 窗口 ----


def test_window_drops_whole_turns_and_never_orphans_a_tool(tmp_path: Path) -> None:
    keeper = SessionStore(root=tmp_path)
    session = keeper.create("stub", "m")
    for index in range(4):
        session.append_user("任务 {}".format(index))
        session.append_assistant("", (ToolCall("c{}".format(index), "shell", "{}"),))
        session.append_tool("c{}".format(index), "输出" * 200)
        session.append_assistant("答复 {}".format(index))
        session.append_turn_end("ok")

    window = session.window(budget=600)

    assert window.dropped > 0
    assert window.messages[0].role == "user"
    assert sum(1 for message in window.messages if message.role == "tool") == sum(
        1 for message in window.messages if message.tool_calls
    )
    assert window.used <= 600
    session.close()


def test_last_turn_is_kept_even_when_it_blows_the_budget() -> None:
    messages = [Message("user", "小" * 10), Message("user", "大" * 10_000)]

    window = fit(messages, budget=10)

    assert window.messages == [Message("user", "大" * 10_000)]
    assert window.note


def test_fit_keeps_everything_under_the_budget() -> None:
    messages = [Message("user", "任务"), Message("assistant", "答复")]

    window = fit(messages, budget=10_000)

    assert window.messages == messages
    assert window.dropped == 0
    assert window.used > 0


def test_last_turns_zero_means_no_cap() -> None:
    messages = [Message("user", "a"), Message("assistant", "b")]

    assert last_turns(messages, 0) == messages
    assert last_turns(messages, 1) == messages


def test_estimate_counts_utf8_bytes() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("汉" * 35) >= 10


# ---- 并发 ----


def test_a_second_owner_is_refused_with_the_pid(tmp_path: Path) -> None:
    first = SessionStore(root=tmp_path)
    session = first.create("stub", "m")

    with pytest.raises(SessionBusy) as info:
        SessionStore(root=tmp_path).open(session.id)

    assert str(__import__("os").getpid()) in str(info.value)
    session.close()
    reopened = SessionStore(root=tmp_path).open(session.id)
    assert reopened is not None
    reopened.close()


def test_two_appends_from_two_handles_keep_distinct_seq(tmp_path: Path) -> None:
    """同一进程里两个句柄各写一条：seq 不重复，行不粘在一起。"""
    keeper = SessionStore(root=tmp_path)
    session = keeper.create("stub", "m")
    session.append_user("第一条")

    with (session.path / "events.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps({"type": "user", "seq": 3, "ts": 1.0, "content": "第二条"})
            + "\n"
        )

    assert [event.seq for event in session.events()] == [1, 2, 3]
    session.close()


# ---- 列表、归档、删除 ----


def test_list_is_ordered_by_last_update(tmp_path: Path) -> None:
    keeper = SessionStore(root=tmp_path)
    first = keeper.create("stub", "m")
    second = keeper.create("stub", "m")
    first.append_user("又动了第一条")

    assert [meta.id for meta in keeper.list()] == [first.id, second.id]
    first.close()
    second.close()


def test_overflow_is_archived_not_deleted(tmp_path: Path) -> None:
    keeper = SessionStore(root=tmp_path, keep=1)
    old = keeper.create("stub", "m")
    old.append_user("旧会话")
    new = keeper.create("stub", "m")

    # 新会话一建好就归档，列表立刻回到 keep 条；再扫一次是空操作。
    assert [meta.id for meta in keeper.list()] == [new.id]
    assert keeper.archive_overflow() == []
    assert (tmp_path / "_archive" / old.id / "events.jsonl").is_file()
    assert old.id in {meta.id for meta in keeper.list(include_archived=True)}


def test_resolve_accepts_an_id_prefix_and_a_title_fragment(tmp_path: Path) -> None:
    keeper = SessionStore(root=tmp_path)
    session = keeper.create("stub", "m")
    session.append_user("整理发布脚本")

    assert keeper.resolve(session.id).id == session.id
    assert keeper.resolve(session.id[:6]).id == session.id
    assert keeper.resolve("发布脚本").id == session.id
    with pytest.raises(SessionError):
        keeper.resolve("不存在的会话")
    session.close()


def test_drop_removes_the_directory_and_the_input_history(
    tmp_path: Path, monkeypatch
) -> None:
    forgotten = []
    monkeypatch.setattr(
        store_module, "forget_session", lambda sid: forgotten.append(sid)
    )
    keeper = SessionStore(root=tmp_path)
    session = keeper.create("stub", "m")
    session_id = session.id

    keeper.drop(session_id)

    assert not (tmp_path / session_id).exists()
    assert forgotten == [session_id]
    assert keeper.current_id() is None


# ---- 分叉 ----


def test_fork_copies_events_up_to_the_given_seq(tmp_path: Path) -> None:
    keeper = SessionStore(root=tmp_path)
    source = keeper.create("stub", "m")
    source.append_user("任务一")
    source.append_assistant("答复一")
    source.append_turn_end("ok")
    cut = source.meta.last_seq
    source.append_user("任务二")

    forked = keeper.fork(source.id, cut)

    assert forked.id != source.id
    assert forked.meta.parent_id == source.id
    assert [message.content for message in forked.history()] == ["任务一", "答复一"]
    assert any(event.type == "fork" for event in forked.events())
    source.close()
    forked.close()


def test_fork_copies_the_blobs_it_points_at(tmp_path: Path) -> None:
    keeper = SessionStore(root=tmp_path)
    source = keeper.create("stub", "m")
    source.append_user("读点东西")
    source.append_assistant("", (ToolCall("c1", "shell", "{}"),))
    source.append_tool("c1", "大" * 20_000)
    source.append_turn_end("ok")

    forked = keeper.fork(source.id)

    # 正文跟着指针走：否则新会话里的这条工具输出只剩「已清理」占位。
    assert [message.content for message in forked.history() if message.role == "tool"] == [
        "大" * 20_000
    ]
    assert list((tmp_path / forked.id / "blobs").iterdir())
    source.close()
    forked.close()


def test_fork_renumbers_the_fields_that_point_at_other_events(tmp_path: Path) -> None:
    keeper = SessionStore(root=tmp_path)
    source = keeper.create("stub", "m")
    source.append_user("旧任务")
    source.append_assistant("旧答复")
    upto = source.meta.last_seq
    source.append_compact("旧任务已经做完，结论是 X", upto)
    snapshot = source.append_screen("$ echo hi\nhi")
    source.append_user("新任务", screen=snapshot.seq)

    forked = keeper.fork(source.id)

    events = forked.events()
    compact = next(event for event in events if event.type == "compact")
    snapshot = next(event for event in events if event.type == "screen")
    task = events[-1]
    # 摘要覆盖到最后一条被压掉的事件（旧答复），不是它自己。
    assert compact.upto_seq == compact.seq - 1
    # 画面与任务跟着改号，否则指回源会话的编号。
    assert task.type == "user" and task.screen == snapshot.seq
    assert forked.history()[-1].content == task_message("新任务", "$ echo hi\nhi")
    source.close()
    forked.close()


def test_fork_keeps_the_turns_a_compaction_left_visible(tmp_path: Path) -> None:
    keeper = SessionStore(root=tmp_path)
    source = keeper.create("stub", "m")
    source.append_user("旧任务")
    source.append_assistant("旧答复")
    source.append_turn_end("ok")
    upto = source.meta.last_seq - 1
    source.append_user("新任务")
    source.append_assistant("新答复")
    source.append_turn_end("ok")
    source.append_compact("旧任务已经做完，结论是 X", upto)
    snapshot = source.append_screen("$ pwd\n/tmp")
    source.append_user("再问一句", screen=snapshot.seq)
    source.append_turn_end("ok")

    forked = keeper.fork(source.id)

    # 摘要之后的轮次还在：新会话里的 cutoff 不能一路盖到摘要自己。
    assert forked.history() == source.history()
    assert forked.meta.turns == source.meta.turns
    assert forked.last_turn_end() is not None
    source.close()
    forked.close()


def test_a_compacted_away_interruption_leaves_no_marker(tmp_path: Path) -> None:
    keeper = SessionStore(root=tmp_path)
    session = keeper.create("stub", "m")
    session.append_user("旧任务")
    session.append_assistant("做了一半")
    session.append_turn_end("aborted")
    upto = session.meta.last_seq - 1
    session.append_user("新任务")
    session.append_turn_end("ok")
    session.append_compact("旧任务做到一半被打断", upto)

    contents = [message.content or "" for message in session.history()]

    # 那一轮的收尾落在 cutoff 之后，但它的任务与答复已经被压掉了。
    assert INTERRUPT_MARKER not in contents
    assert "新任务" in contents
    session.close()


def test_fit_counts_the_pending_task_against_the_budget() -> None:
    messages = [Message("user", "旧" * 400), Message("assistant", "答" * 400)]
    pending = [Message("user", "新任务")]

    window = fit(messages, budget=200, pending=pending)

    # 旧轮次放不进预算就整轮丢掉 —— 当前任务一定发得出去，且不重复。
    assert window.messages == []
    assert window.keep_from == len(messages)
    assert window.dropped == 2
    assert window.used < 200


def test_fit_keeps_the_last_turn_when_nothing_is_pending() -> None:
    messages = [Message("user", "旧" * 400), Message("assistant", "答" * 400)]

    window = fit(messages, budget=200)

    assert window.messages == messages
    assert window.keep_from == 0


def test_current_pointer_is_only_a_cache(tmp_path: Path) -> None:
    keeper = SessionStore(root=tmp_path)
    session = keeper.create("stub", "m")
    session.close()

    index = tmp_path / "index.json"
    index.unlink()

    reopened = SessionStore(root=tmp_path)
    assert reopened.current_id() is None
    assert repr(reopened.list()[0].id) == repr(session.id)


def test_a_continued_turn_opens_its_own_entry(tmp_path: Path) -> None:
    """``/retry`` 接着跑的那一段没有 user 事件：答复与收尾仍要折进覆盖层。

    既不能丢（旧写法只认「手上有任务」），也不能并进上一轮 —— 中断前已经
    写下的那句答复，``/status`` 还要翻得到。
    """
    keeper = SessionStore(root=tmp_path)
    session = keeper.create("stub", "m")
    session.append_user("任务")
    session.append_assistant("答复一")
    session.append_turn_end("aborted", "已中断")
    session.append_assistant("答复二")
    session.append_turn_end("ok")

    assert session.log(2) == [("任务", "答复一", "已中断"), ("任务", "答复二", "")]
    session.close()
