"""命令层与会话存储的接缝：事件真的落了盘、跨进程能续、窗口能解释。"""

from __future__ import annotations

import asyncio
import json

from lls.ai import Chunk, ChunkKind, ConfigError
from lls.ai.sessions import SessionBusy, SessionStore
from lls.ai.sessions.events import INTERRUPT_MARKER
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
    # 常规一轮不写备注：条数与预算由 /context 算，耗时由前后两条 ts 相减。
    assert events[3].note == ""
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


def test_an_aborted_turn_stays_in_context_marked_as_interrupted(monkeypatch, tmp_path) -> None:
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
    # 中断的一轮留在上下文里，后面跟一行标记：下一轮不会当它没发生过。
    sent = provider.requests[-1]
    user_texts = [message.content or "" for message in sent if message.role == "user"]
    assert any(text.startswith("任务一") for text in user_texts)
    assert INTERRUPT_MARKER in user_texts
    session.close()


def test_an_interrupted_reply_keeps_what_was_already_shown(monkeypatch, tmp_path) -> None:
    """中断时正文还没收完：屏幕上已经显示的，上下文里也要有。"""
    keeper = SessionStore(root=tmp_path)

    class SlowProvider(ScriptedProvider):
        def __init__(self) -> None:
            super().__init__([])
            self.sent = 0

        async def chat(self, model, messages, *, tools=None, **kwargs):
            self.requests.append(list(messages))
            for piece in ("先看", "一下", "终端"):
                await asyncio.sleep(0)
                self.sent += 1
                yield Chunk(ChunkKind.TEXT, piece)
            yield Chunk(ChunkKind.DONE)

    provider = SlowProvider()
    services = FakeServices(["任务", "任务二", "/exit"])

    async def read_chars(timeout=None):
        await asyncio.sleep(0)
        # 第一轮吐了两段就中断；第二轮（任务二）正常跑完。
        interrupted = len(services.prompts) == 1 and provider.sent >= 2
        return b"\x03" if interrupted else b""

    services.read_chars = read_chars
    monkeypatch.setattr(agent_cmd, "default_catalog", lambda: FakeCatalog(provider))
    monkeypatch.setattr(agent_cmd, "store", lambda: keeper)
    asyncio.run(agent_cmd.cmd_agent(services, None))

    session = keeper.current()
    partial = [event.content for event in session.events() if event.type == "assistant"]
    assert partial and "先看一下终端".startswith(partial[0]) and partial[0]
    # 下一轮的请求里带着这段正文，紧接着才是中断标记。
    sent = provider.requests[-1]
    texts = [message.content or "" for message in sent]
    assert partial[0] in texts
    assert texts.index(partial[0]) < texts.index(INTERRUPT_MARKER)
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


def _prefill_sized(keeper, turns: int = 17, chars: int = 700) -> object:
    """造一段「装得下、但已经到水位」的历史：每轮约 610 tok。"""
    session = keeper.create("stub", "m")
    for index in range(turns):
        session.append_user("旧任务 {}".format(index))
        session.append_assistant("答" * chars)
        session.append_turn_end("ok")
    return session


def test_the_watermark_compacts_before_anything_is_dropped(monkeypatch, tmp_path) -> None:
    """到 85% 就压，不必等到装不下：等到装不下，窗口就永远贴着上限。

    预算 12000（窗口 20000），水位 10200，目标 7200。
    """
    keeper = SessionStore(root=tmp_path)
    _prefill_sized(keeper).close()
    provider = ScriptedProvider([_text("摘要：旧任务都做完了"), _text("新答复")])

    _run(
        monkeypatch,
        provider,
        ["/exit"],
        keeper,
        args="新任务",
        catalog=FakeCatalog(provider, 20000),
    )

    session = keeper.current()
    compacts = [event for event in session.events() if event.type == "compact"]
    assert len(compacts) == 1
    # 压完停在目标水位以下，而不是刚好够这一次请求。
    window = session.window(12000)
    assert window.dropped == 0
    assert window.used <= 7200
    # 留下的还是最近几轮旧历史 + 这一轮，不是只留一条摘要。
    assert sum(1 for m in session.history() if m.role == "user") >= 10
    # 这一轮真压过，才在覆盖层留一句话。
    assert "已压缩到" in session.last_turn_end().note
    session.close()


def test_the_next_turns_do_not_compact_again(monkeypatch, tmp_path) -> None:
    """压过一次以后留出几轮空隙：否则每轮都要重摘一遍整段前缀。"""
    keeper = SessionStore(root=tmp_path)
    _prefill_sized(keeper).close()
    provider = ScriptedProvider([_text("摘要：旧任务都做完了")] + [_text("新答复")] * 5)

    _run(
        monkeypatch,
        provider,
        ["任务1", "任务2", "任务3", "/exit"],
        keeper,
        catalog=FakeCatalog(provider, 20000),
    )

    session = keeper.current()
    compacts = [event for event in session.events() if event.type == "compact"]
    assert len(compacts) == 1
    session.close()


def test_a_later_compaction_merges_the_previous_summary(monkeypatch, tmp_path) -> None:
    """第二次压缩以上一版摘要为底稿，不重摘整段前缀。"""

    class Summarising(ScriptedProvider):
        def __init__(self, rounds) -> None:
            super().__init__(rounds)
            self.summaries: list = []

        async def chat(self, model, messages, **kwargs):
            if any(
                message.role == "system" and "压缩成要点" in (message.content or "")
                for message in messages
            ):
                self.summaries.append(messages[-1].content or "")
                yield Chunk(ChunkKind.TEXT, "摘要第{}版".format(len(self.summaries)))
                yield Chunk(ChunkKind.DONE)
                return
            yield Chunk(ChunkKind.TEXT, "答" * 700)
            yield Chunk(ChunkKind.DONE)

    keeper = SessionStore(root=tmp_path)
    _prefill_sized(keeper).close()
    provider = Summarising([])

    _run(
        monkeypatch,
        provider,
        ["任务{}".format(index) for index in range(9)] + ["/exit"],
        keeper,
        catalog=FakeCatalog(provider, 20000),
    )

    session = keeper.current()
    cutoffs = [event.upto_seq for event in session.events() if event.type == "compact"]
    assert len(cutoffs) >= 2
    assert cutoffs == sorted(set(cutoffs))  # 每次都比上一次更远，不原地打转
    assert "上一版摘要：" not in provider.summaries[0]
    assert "上一版摘要：" in provider.summaries[1]
    assert "摘要第1版" in provider.summaries[1]
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
