"""agent 会话的持久化：存储本身，以及跨 `agent` 调用的上下文延续。

会话是独立实体（有自己的 id），provider/model 只是它的字段；这里同时覆盖
「存储语义」和「命令层是否真的把状态接上」。
"""

from __future__ import annotations

import asyncio
import json

from lls.ai import Chunk, ChunkKind, ConfigError, Message, ToolCall
from lls.commands.builtin import agent as agent_cmd
from lls.commands.builtin.agent_store import (
    SessionStore,
    default_sessions_dir,
    migrate_legacy_sessions,
)

# ---- 桩 ----


class FakeScreen:
    columns = 80
    rows = 24
    alternate = False

    def text(self, **kwargs) -> str:
        return "screen"


class FakeServices:
    def __init__(self, answers=()) -> None:
        self.screen = FakeScreen()
        self._answers = list(answers)
        self.prompts = []
        self.messages = []

    async def read_line(self, prompt, **kwargs):
        self.prompts.append(prompt)
        return self._answers.pop(0) if self._answers else None

    async def read_chars(self, timeout=None):
        await asyncio.sleep(0)
        return b""

    def draw_overlay(self, text, cursor=None):
        return 0, len(text.split("\n"))

    def flush_model(self) -> None:
        pass

    def restore_context(self) -> None:
        pass

    def requeue_pending_keys(self) -> None:
        pass

    def write(self, data, enter=False) -> None:
        pass

    async def show_message(self, text) -> None:
        self.messages.append(text)


class FakeCatalog:
    def __init__(self, provider, spec=("stub", "stub-model")) -> None:
        self._provider = provider
        self._spec = spec

    def resolve_spec(self):
        return self._spec

    def get(self, provider_id):
        if provider_id != self._spec[0]:
            raise ConfigError("no such provider %r" % provider_id)
        return self._provider


class ScriptedProvider:
    def __init__(self, rounds) -> None:
        self.rounds = list(rounds)
        self.requests = []

    async def chat(self, model, messages, *, tools=None, **kwargs):
        self.requests.append({"model": model, "messages": list(messages)})
        for chunk in self.rounds.pop(0):
            yield chunk


class AbortingProvider:
    """流式输出到一半被子进程结束打断（Ctrl-C 走的是同一条中断路径）。"""

    async def chat(self, model, messages, **kwargs):
        yield Chunk(ChunkKind.TEXT, "半成品")
        raise RuntimeError("boom")


def _text(value: str):
    return [Chunk(ChunkKind.TEXT, value), Chunk(ChunkKind.DONE)]


def _run(monkeypatch, provider, answers, *, first_task=None, keeper=None, catalog=None):
    services = FakeServices(answers)
    monkeypatch.setattr(
        agent_cmd, "default_catalog", lambda: catalog or FakeCatalog(provider)
    )
    keeper = keeper if keeper is not None else SessionStore()
    monkeypatch.setattr(agent_cmd, "store", lambda: keeper)
    result = asyncio.run(agent_cmd.cmd_agent(services, first_task))
    return services, result, keeper


# ---- 存储语义 ----


def test_new_session_becomes_current_and_gets_a_unique_id() -> None:
    keeper = SessionStore()

    first = keeper.new("openai", "gpt-4o-mini")
    second = keeper.new("openai", "gpt-4o-mini")

    assert first.id != second.id
    assert keeper.current() is second
    assert [state.id for state in keeper.list()] == [second.id, first.id]
    assert first.model == "openai/gpt-4o-mini"


def test_dropping_the_current_session_clears_the_pointer() -> None:
    keeper = SessionStore()
    state = keeper.new("openai", "m")

    assert keeper.drop(state.id) is True
    assert keeper.drop(state.id) is False
    assert keeper.current() is None
    assert keeper.get(state.id) is None


def test_store_keeps_the_most_recently_used_sessions() -> None:
    keeper = SessionStore(keep=2)
    first = keeper.new("a", "m")
    second = keeper.new("a", "m")
    third = keeper.new("a", "m")
    first.updated_at, second.updated_at, third.updated_at = 1.0, 2.0, 3.0

    keeper.trim()

    assert [state.id for state in keeper.list()] == [third.id, second.id]
    assert keeper.get(first.id) is None


def test_turns_counts_user_messages() -> None:
    from lls.commands.builtin.agent_store import SessionState

    state = SessionState(
        history=[Message("user", "t"), Message("assistant", "a"), Message("user", "t2")]
    )

    assert state.turns == 2


# ---- 跨调用延续 ----


def test_reentering_agent_continues_the_previous_session(monkeypatch) -> None:
    keeper = SessionStore()
    first_provider = ScriptedProvider([_text("答复一")])
    _, result, _ = _run(
        monkeypatch, first_provider, ["/exit"], first_task="任务一", keeper=keeper
    )
    assert result.handler == "default"
    state = keeper.current()
    assert state is not None and state.turns == 1

    second_provider = ScriptedProvider([_text("答复二")])
    _run(monkeypatch, second_provider, ["/exit"], first_task="任务二", keeper=keeper)

    sent = second_provider.requests[0]["messages"]
    assert any("任务一" in (message.content or "") for message in sent)
    assert any("答复一" in (message.content or "") for message in sent)
    assert keeper.current().id == state.id


def test_new_flag_starts_a_fresh_session_and_keeps_the_old_one(monkeypatch) -> None:
    keeper = SessionStore()
    first_provider = ScriptedProvider([_text("答复一")])
    _run(monkeypatch, first_provider, ["/exit"], first_task="任务一", keeper=keeper)
    old = keeper.current()

    second_provider = ScriptedProvider([_text("答复二")])
    _run(monkeypatch, second_provider, ["/exit"], first_task="--new 任务二", keeper=keeper)

    assert len(keeper) == 2
    assert keeper.current().id != old.id
    sent = second_provider.requests[0]["messages"]
    assert not any("任务一" in (message.content or "") for message in sent)
    assert keeper.get(old.id) is not None


def test_tasks_are_recorded_per_session_not_globally(monkeypatch) -> None:
    keeper = SessionStore()
    provider = ScriptedProvider([_text("a1"), _text("b1"), _text("a2")])

    _run(monkeypatch, provider, ["/exit"], first_task="任务A1", keeper=keeper)
    session_a = keeper.current()
    _run(monkeypatch, provider, ["/exit"], first_task="--new 任务B1", keeper=keeper)
    session_b = keeper.current()
    keeper.set_current(session_a.id)
    _run(monkeypatch, provider, ["/exit"], first_task="任务A2", keeper=keeper)

    assert session_a.turns == 2
    assert session_b.turns == 1
    assert [state.id for state in keeper.list()][-1] == session_b.id


def test_provider_removed_from_config_is_reported_and_state_kept(monkeypatch) -> None:
    keeper = SessionStore()
    _run(
        monkeypatch,
        ScriptedProvider([_text("答复")]),
        ["/exit"],
        first_task="任务",
        keeper=keeper,
    )
    state = keeper.current()
    state.provider_id = "gone"

    services, result, _ = _run(
        monkeypatch, ScriptedProvider([]), [], keeper=keeper
    )

    assert result.handler is None
    assert any("gone" in text for text in services.messages)
    assert any("agent --new" in text for text in services.messages)
    assert keeper.get(state.id) is not None


# ---- 写回时机 ----


def test_a_failed_round_is_not_written_back(monkeypatch) -> None:
    keeper = SessionStore()
    services, result, _ = _run(
        monkeypatch, AbortingProvider(), ["/exit"], first_task="任务", keeper=keeper
    )

    assert result.handler == "default"
    state = keeper.current()
    assert state.history == []
    assert state.log == []


def test_history_written_back_is_already_trimmed(monkeypatch) -> None:
    monkeypatch.setattr(agent_cmd, "HISTORY_TURNS", 2)
    keeper = SessionStore()
    provider = ScriptedProvider([_text("a1"), _text("a2"), _text("a3")])

    _run(
        monkeypatch,
        provider,
        ["/exit"],
        first_task="任务1",
        keeper=keeper,
    )
    _run(
        monkeypatch,
        provider,
        ["/exit"],
        first_task="任务2",
        keeper=keeper,
    )

    # 存进去的就是已裁剪的历史（HISTORY_TURNS - 1 轮），恢复时长与连续会话一致
    assert keeper.current().turns == 1
    assert any(
        "任务2" in (message.content or "") for message in keeper.current().history
    )


def test_reentering_does_not_replay_the_log_twice(monkeypatch) -> None:
    keeper = SessionStore()
    provider = ScriptedProvider([_text("答复一"), _text("答复二")])
    _run(monkeypatch, provider, ["/exit"], first_task="任务一", keeper=keeper)
    services, _, _ = _run(
        monkeypatch, provider, ["/exit"], first_task="任务二", keeper=keeper
    )

    prompt = services.prompts[0]
    assert prompt.count("任务一") == 1
    assert prompt.count("答复一") == 1


def test_session_log_survives_a_restart_of_the_session_object(monkeypatch) -> None:
    keeper = SessionStore()
    provider = ScriptedProvider([_text("答复一"), _text("答复二")])
    _run(monkeypatch, provider, ["/exit"], first_task="任务一", keeper=keeper)
    services, _, _ = _run(
        monkeypatch, provider, ["/exit"], first_task="任务二", keeper=keeper
    )

    assert "任务一" in services.prompts[0]
    assert "答复一" in services.prompts[0]


# ---- 落盘 ----


def test_sessions_are_written_to_and_read_from_disk(tmp_path) -> None:
    keeper = SessionStore(root=tmp_path)
    state = keeper.new("stub", "stub-model")
    state.history = [
        Message("user", "任务"),
        Message(
            "assistant",
            None,
            tool_calls=(
                ToolCall(id="c1", name="read_terminal", arguments='{"lines": 5}'),
            ),
        ),
        Message("tool", "画面", tool_call_id="c1"),
        Message("assistant", "答复"),
    ]
    state.log = [("任务", "答复", "")]
    state.updated_at = 123.0
    keeper.save(state)

    assert sorted(path.name for path in tmp_path.glob("*.json")) == sorted(
        [state.id + ".json", "index.json"]
    )
    assert not list(tmp_path.glob("*.tmp"))

    reopened = SessionStore(root=tmp_path)
    loaded = reopened.get(state.id)
    assert loaded is not None
    assert loaded.history == state.history
    assert loaded.log == state.log
    assert loaded.created_at == state.created_at
    assert loaded.updated_at == 123.0
    assert reopened.current() is not None
    assert reopened.current().id == state.id


def test_current_pointer_survives_a_restart(tmp_path) -> None:
    keeper = SessionStore(root=tmp_path)
    first = keeper.new("stub", "m")
    keeper.new("stub", "m")
    keeper.set_current(first.id)

    reopened = SessionStore(root=tmp_path)

    assert reopened.current() is not None
    assert reopened.current().id == first.id


def test_corrupt_and_unknown_files_are_skipped(tmp_path) -> None:
    (tmp_path / "aaaaaa.json").write_text("{oops", encoding="utf-8")
    (tmp_path / "bbbbbb.json").write_text(
        json.dumps(
            {"version": 999, "id": "bbbbbb", "provider_id": "p", "model_id": "m"}
        ),
        encoding="utf-8",
    )
    (tmp_path / "cccccc.json.tmp").write_text("{}", encoding="utf-8")

    keeper = SessionStore(root=tmp_path)

    assert len(keeper) == 0
    assert keeper.current() is None
    assert keeper.last_error is None


def test_loading_enforces_the_session_cap_and_deletes_files(tmp_path) -> None:
    keeper = SessionStore(root=tmp_path)
    first = keeper.new("a", "m")
    second = keeper.new("a", "m")
    third = keeper.new("a", "m")
    for offset, state in enumerate((first, second, third), start=1):
        state.updated_at = float(offset)
        keeper.save(state)

    reopened = SessionStore(keep=2, root=tmp_path)

    assert [state.id for state in reopened.list()] == [third.id, second.id]
    assert not (tmp_path / (first.id + ".json")).exists()
    assert (tmp_path / (second.id + ".json")).exists()


def test_a_failed_write_degrades_to_memory(tmp_path) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")

    keeper = SessionStore(root=blocker)
    state = keeper.new("a", "m")

    assert keeper.get(state.id) is state
    assert keeper.last_error


def test_agent_continues_from_disk_after_a_restart(monkeypatch, tmp_path) -> None:
    first = SessionStore(root=tmp_path)
    provider = ScriptedProvider([_text("答复一")])
    _run(monkeypatch, provider, ["/exit"], first_task="任务一", keeper=first)
    session_id = first.current().id

    reopened = SessionStore(root=tmp_path)
    second = ScriptedProvider([_text("答复二")])
    services, result, _ = _run(
        monkeypatch, second, ["/exit"], first_task="任务二", keeper=reopened
    )

    assert result.handler == "default"
    sent = second.requests[0]["messages"]
    assert any("任务一" in (message.content or "") for message in sent)
    assert any("答复一" in (message.content or "") for message in sent)
    assert "任务一" in services.prompts[0]
    assert reopened.current().id == session_id


def test_default_sessions_dir_follows_the_environment(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setenv("LLS_SESSIONS_HOME", str(tmp_path))

    assert default_sessions_dir() == tmp_path / "sessions"


def test_default_sessions_dir_is_not_under_state(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("LLS_SESSIONS_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    assert default_sessions_dir() == tmp_path / ".local" / "lls" / "sessions"


def test_legacy_sessions_are_copied_on_first_use(tmp_path) -> None:
    old = tmp_path / "old"
    old.mkdir()
    (old / "abc123.json").write_text("{}", encoding="utf-8")
    (old / "index.json").write_text("{}", encoding="utf-8")
    target = tmp_path / "new" / "sessions"

    assert migrate_legacy_sessions(target, source=old) == 2
    assert sorted(p.name for p in target.glob("*.json")) == ["abc123.json", "index.json"]
    # 旧目录只读不改。
    assert (old / "abc123.json").exists()
    # 只搬一次：新目录已经在就不动。
    (target / "abc123.json").write_text("mine", encoding="utf-8")
    assert migrate_legacy_sessions(target, source=old) == 0
    assert (target / "abc123.json").read_text(encoding="utf-8") == "mine"


def test_legacy_migration_skips_missing_source(tmp_path) -> None:
    assert migrate_legacy_sessions(tmp_path / "sessions", source=tmp_path / "nope") == 0
    assert not (tmp_path / "sessions").exists()
