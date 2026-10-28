"""命令层与会话存储的接缝：事件真的落了盘、跨进程能续、窗口能解释。"""

from __future__ import annotations

import asyncio
import json

from lls.ai import Chunk, ChunkKind, ConfigError
from lls.ai.sessions import SessionBusy, SessionStore
from lls.commands.builtin import agent as agent_cmd
from lls.commands.builtin import sessions as sessions_cmd


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
    def __init__(self, provider, window: int = 128_000) -> None:
        self._provider = provider
        self._window = window

    def resolve_spec(self, spec=None):
        return "stub", "stub-model"

    def get(self, provider_id):
        return self._provider

    def provider_config(self, provider_id):
        class _Conf:
            context_window = self._window

        return _Conf()

    def context_limit(self):
        return 0


class ScriptedProvider:
    id = "stub"

    def __init__(self, rounds) -> None:
        self.rounds = list(rounds)
        self.requests = []

    async def chat(self, model, messages, *, tools=None, **kwargs):
        self.requests.append(list(messages))
        for chunk in self.rounds.pop(0):
            yield chunk


def _text(value: str, usage=None):
    chunks = [Chunk(ChunkKind.TEXT, value)]
    if usage:
        chunks.append(Chunk(ChunkKind.USAGE, usage=usage))
    chunks.append(Chunk(ChunkKind.DONE))
    return chunks


def _run(monkeypatch, provider, answers, keeper, *, args=None, catalog=None):
    services = FakeServices(answers)
    monkeypatch.setattr(
        agent_cmd, "default_catalog", lambda: catalog or FakeCatalog(provider)
    )
    monkeypatch.setattr(agent_cmd, "store", lambda: keeper)
    result = asyncio.run(agent_cmd.cmd_agent(services, args))
    return services, result


# ---- 事件真的落了盘 ----


def test_a_completed_turn_writes_events_with_usage(monkeypatch, tmp_path) -> None:
    keeper = SessionStore(root=tmp_path)
    provider = ScriptedProvider([_text("答复", {"prompt": 9, "completion": 4})])

    _run(monkeypatch, provider, ["/exit"], keeper, args="任务")

    session = keeper.current()
    assert session is not None
    events = session.events()
    assert [event.type for event in events] == [
        "session_start",
        "user",
        "assistant",
        "turn_end",
    ]
    assert events[2].usage == {"prompt": 9, "completion": 4}
    assert events[3].status == "ok"
    assert "tok" in events[3].note
    assert session.meta.turns == 1
    session.close()


def test_the_transcript_survives_into_the_next_process(monkeypatch, tmp_path) -> None:
    first = SessionStore(root=tmp_path)
    _run(monkeypatch, ScriptedProvider([_text("答复一")]), ["/exit"], first, args="任务一")
    first.current().close()

    reopened = SessionStore(root=tmp_path)
    provider = ScriptedProvider([_text("答复二")])
    _run(monkeypatch, provider, ["/exit"], reopened, args="任务二")

    sent = provider.requests[0]
    assert [message.role for message in sent] == [
        "system",
        "user",
        "assistant",
        "user",
    ]
    assert "任务一" in (sent[1].content or "")
    assert "答复一" in (sent[2].content or "")


def test_an_aborted_turn_is_recorded_but_not_sent(monkeypatch, tmp_path) -> None:
    keeper = SessionStore(root=tmp_path)
    provider = ScriptedProvider([_text("答复一"), _text("答复二")])
    services = FakeServices(["任务一", "任务二", "/exit"])
    services.queue = None

    async def read_chars(timeout=None):
        await asyncio.sleep(0)
        # 第一轮读到 Ctrl-C，第二轮正常。
        return b"\x03" if len(services.prompts) == 1 else b""

    services.read_chars = read_chars
    monkeypatch.setattr(agent_cmd, "default_catalog", lambda: FakeCatalog(provider))
    monkeypatch.setattr(agent_cmd, "store", lambda: keeper)
    asyncio.run(agent_cmd.cmd_agent(services, None))

    session = keeper.current()
    statuses = [event.status for event in session.events() if event.type == "turn_end"]
    assert statuses == ["aborted", "ok"]
    sent = provider.requests[-1]
    assert all("任务一" not in (message.content or "") for message in sent if message.role == "user")
    session.close()


# ---- 参数、/context、/title ----


def test_session_list_shows_id_title_and_turns(monkeypatch, tmp_path) -> None:
    keeper = SessionStore(root=tmp_path)
    _run(monkeypatch, ScriptedProvider([_text("答复")]), ["/exit"], keeper, args="整理发布脚本")

    services, _ = _run(monkeypatch, ScriptedProvider([]), [], keeper, args="--list")

    listing = services.messages[-1]
    assert keeper.current_id() in listing
    assert "整理发布脚本" in listing


def test_resume_and_title_flags(monkeypatch, tmp_path) -> None:
    keeper = SessionStore(root=tmp_path)
    _run(monkeypatch, ScriptedProvider([_text("答复")]), ["/exit"], keeper, args="任务一")
    session_id = keeper.current_id()

    monkeypatch.setattr(agent_cmd, "store", lambda: keeper)
    services = FakeServices([])
    monkeypatch.setattr(agent_cmd, "default_catalog", lambda: FakeCatalog(ScriptedProvider([])))
    asyncio.run(
        agent_cmd.cmd_agent(services, "--resume {} --title 新标题".format(session_id[:6]))
    )

    assert keeper.open(session_id).meta.title == "新标题"


def test_context_reports_the_window(monkeypatch, tmp_path) -> None:
    keeper = SessionStore(root=tmp_path)
    _run(monkeypatch, ScriptedProvider([_text("答复")]), ["/exit"], keeper, args="任务一")

    services = FakeServices([])
    monkeypatch.setattr(agent_cmd, "store", lambda: keeper)
    monkeypatch.setattr(agent_cmd, "default_catalog", lambda: FakeCatalog(ScriptedProvider([_text("答复")])))
    services._answers = ["/context", "/exit"]
    asyncio.run(agent_cmd.cmd_agent(services, None))

    report = services.messages[-1]
    assert "窗口" in report
    assert "轮" in report


def test_a_busy_session_is_reported_instead_of_stolen(monkeypatch, tmp_path) -> None:
    keeper = SessionStore(root=tmp_path)
    session = keeper.create("stub", "m")
    session.close()
    other = SessionStore(root=tmp_path).open(session.id)
    assert other is not None

    services = FakeServices([])
    monkeypatch.setattr(agent_cmd, "store", lambda: SessionStore(root=tmp_path))
    monkeypatch.setattr(agent_cmd, "default_catalog", lambda: FakeCatalog(ScriptedProvider([])))
    asyncio.run(agent_cmd.cmd_agent(services, None))

    assert "占用" in services.messages[-1]
    other.close()


def test_a_missing_provider_names_the_session(monkeypatch, tmp_path) -> None:
    keeper = SessionStore(root=tmp_path)
    session = keeper.create("gone", "m")

    class Missing:
        def resolve_spec(self, spec=None):
            return "gone", "m"

        def get(self, provider_id):
            raise ConfigError("no such provider 'gone'")

        def provider_config(self, provider_id):
            raise ConfigError("no such provider 'gone'")

        def context_limit(self):
            return 0

    services = FakeServices([])
    monkeypatch.setattr(agent_cmd, "store", lambda: keeper)
    monkeypatch.setattr(agent_cmd, "default_catalog", lambda: Missing())
    asyncio.run(agent_cmd.cmd_agent(services, "--resume " + session.id))

    assert "gone" in services.messages[-1]
    session.close()


# ---- sessions / export / fork / rm ----


def test_sessions_command_lists_and_exports(monkeypatch, tmp_path) -> None:
    keeper = SessionStore(root=tmp_path)
    session = keeper.create("stub", "m")
    session.append_user("任务")
    session.append_assistant("答复")
    session.close()
    monkeypatch.setattr(sessions_cmd, "store", lambda: keeper)

    services = FakeServices([])
    asyncio.run(sessions_cmd.cmd_sessions(services, None))
    assert session.id in services.messages[-1]

    asyncio.run(sessions_cmd.cmd_export(services, "{} --jsonl".format(session.id)))
    exported = [json.loads(line) for line in services.messages[-1].splitlines()]
    assert [event["type"] for event in exported][-2:] == ["user", "assistant"]
    assert exported[-1]["content"] == "答复"


def test_fork_command_copies_up_to_seq(monkeypatch, tmp_path) -> None:
    keeper = SessionStore(root=tmp_path)
    session = keeper.create("stub", "m")
    session.append_user("任务")
    cut = session.meta.last_seq
    session.append_assistant("答复")
    session.close()
    monkeypatch.setattr(sessions_cmd, "store", lambda: keeper)

    services = FakeServices([])
    asyncio.run(sessions_cmd.cmd_fork(services, "{} --at {}".format(session.id, cut)))

    forked = keeper.current()
    assert forked.id != session.id
    assert [message.content for message in forked.history()] == ["任务"]
    forked.close()


def test_rm_command_removes_the_session(monkeypatch, tmp_path) -> None:
    keeper = SessionStore(root=tmp_path)
    session = keeper.create("stub", "m")
    session.close()
    monkeypatch.setattr(sessions_cmd, "store", lambda: keeper)

    services = FakeServices([])
    asyncio.run(sessions_cmd.cmd_rm(services, session.id))

    assert not (tmp_path / session.id).exists()
    assert "已删除" in services.messages[-1]


# ---- 压缩 ----


def _prefill(keeper, turns: int = 3) -> object:
    session = keeper.create("stub", "m")
    for index in range(turns):
        session.append_user("旧任务 {} {}".format(index, "细" * 400))
        session.append_assistant("旧答复 {} {}".format(index, "节" * 400))
        session.append_turn_end("ok")
    return session


def test_old_turns_are_summarised_before_they_are_dropped(monkeypatch, tmp_path) -> None:
    keeper = SessionStore(root=tmp_path)
    _prefill(keeper).close()
    provider = ScriptedProvider([_text("摘要：之前在做旧任务"), _text("新答复")])

    _run(
        monkeypatch,
        provider,
        ["/exit"],
        keeper,
        args="新任务",
        catalog=FakeCatalog(provider, 400),
    )

    session = keeper.current()
    compacts = [event for event in session.events() if event.type == "compact"]
    assert compacts and compacts[0].content == "摘要：之前在做旧任务"
    assert compacts[0].upto_seq > 0
    # 真正那轮请求以摘要开头，旧任务不再逐条重发。
    sent = provider.requests[-1]
    assert "摘要：之前在做旧任务" in (sent[1].content or "")
    assert all("细" * 400 not in (message.content or "") for message in sent)
    session.close()


def test_every_round_can_compact_further_into_the_transcript(monkeypatch, tmp_path) -> None:
    """压缩的 cutoff 必须每轮前进：拿消息条数相减会永远少裁一条。"""

    class Looping(ScriptedProvider):
        async def chat(self, model, messages, **kwargs):
            if any(
                message.role == "system" and "压缩成要点" in (message.content or "")
                for message in messages
            ):
                yield Chunk(ChunkKind.TEXT, "摘要到此")
                yield Chunk(ChunkKind.DONE)
                return
            yield Chunk(ChunkKind.TEXT, "答" * 2000)
            yield Chunk(ChunkKind.DONE)

    keeper = SessionStore(root=tmp_path)
    provider = Looping([])

    _run(
        monkeypatch,
        provider,
        ["任务1", "任务2", "任务3", "/exit"],
        keeper,
        catalog=FakeCatalog(provider, 400),
    )

    session = keeper.current()
    compacts = [event for event in session.events() if event.type == "compact"]
    assert len(compacts) >= 2
    cutoffs = [event.upto_seq for event in compacts]
    assert cutoffs == sorted(set(cutoffs))
    assert cutoffs[-1] > cutoffs[0]
    # 压缩是累积的：历史里只剩一段摘要加上之后没被裁掉的部分。
    assert sum(1 for message in session.history() if message.role == "user") == 2
    session.close()


def test_a_failed_summary_falls_back_to_a_hard_cut(monkeypatch, tmp_path) -> None:
    class Failing(ScriptedProvider):
        """第一次请求（摘要）直接失败，之后的真实请求照常走脚本。"""

        failed = False

        async def chat(self, model, messages, **kwargs):
            if not self.failed:
                self.failed = True
                raise RuntimeError("summary failed")
            async for chunk in super().chat(model, messages, **kwargs):
                yield chunk

    keeper = SessionStore(root=tmp_path)
    _prefill(keeper).close()
    provider = Failing([_text("新答复")])

    _run(
        monkeypatch,
        provider,
        ["/exit"],
        keeper,
        args="新任务",
        catalog=FakeCatalog(provider, 400),
    )

    session = keeper.current()
    # 摘要失败就退回硬截断：没有 compact 事件，但这一轮照常完成并入库。
    assert not [event for event in session.events() if event.type == "compact"]
    assert session.log()[-1][:2] == ("新任务", "新答复")
    assert session.last_turn_end().status == "ok"
    session.close()
