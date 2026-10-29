"""命令层与会话存储的接缝：事件真的落了盘、跨进程能续、窗口能解释。"""

from __future__ import annotations

import asyncio

from lls.ai import Chunk, ChunkKind, ConfigError
from lls.ai.sessions import SessionBusy, SessionStore
from lls.ai.sessions.events import INTERRUPT_MARKER
from lls.commands.builtin import agent as agent_cmd
from lls.commands.builtin import sessions as sessions_cmd
from lls.tty import Key


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
        self.painted = []
        self._key = None

    async def read_line(self, prompt, **kwargs):
        self.prompts.append(prompt)
        return self._answers.pop(0) if self._answers else None

    async def read_chars(self, timeout=None):
        await asyncio.sleep(0)
        return b""

    async def read_key(self):
        await asyncio.sleep(0)
        return self._key or Key(control="escape", raw=b"\x1b")

    def draw_overlay(self, text, cursor=None):
        self.painted.append(text)
        return 0, len(text.split("\n"))

    def set_overlay_source(self, source=None) -> None:
        pass

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
        "screen",
        "user",
        "assistant",
        "turn_end",
    ]
    # 画面先落盘，任务那条指向它：重放时才有当时发出去的那段正文。
    assert events[1].content == "screen"
    assert events[2].screen == events[1].seq
    assert events[3].usage == {"prompt": 9, "completion": 4}
    assert events[4].status == "ok"
    # 常规一轮不写备注：条数与预算由 /status 看，耗时由前后两条 ts 相减。
    assert events[4].note == ""
    assert session.meta.turns == 1
    session.close()


def test_retry_continues_without_adding_a_message(monkeypatch, tmp_path) -> None:
    """``/retry`` 只接着跑：不新增消息，也不多写一条 user。"""
    keeper = SessionStore(root=tmp_path)
    provider = ScriptedProvider([_text("答复一"), _text("答复二")])

    _run(monkeypatch, provider, ["任务一", "/retry", "/exit"], keeper)

    session = keeper.current()
    events = session.events()
    assert [event.type for event in events] == [
        "session_start",
        "screen",
        "user",
        "assistant",
        "turn_end",
        "assistant",
        "turn_end",
    ]
    # 第二次请求里没有新的用户消息：会话原样继续。
    sent = provider.requests[-1]
    assert [m.role for m in sent if m.role != "system"] == ["user", "assistant"]
    session.close()


def test_retry_paints_its_reply_into_the_prompt(monkeypatch, tmp_path) -> None:
    """``/retry`` 之后提示层要换成接着跑出来的答复。

    旧写法只把「手上有任务」的答复折进覆盖层：接着跑的那一轮没有新的 user
    事件，答复与收尾都被丢掉，提示行一直停在中断前的样子 —— 看起来就是
    ``/retry`` 什么也没刷新。
    """
    keeper = SessionStore(root=tmp_path)
    provider = ScriptedProvider([_text("答复一"), _text("答复二")])

    services, _ = _run(monkeypatch, provider, ["任务一", "/retry", "/exit"], keeper)

    assert "答复二" in services.prompts[-1]
    assert "答复一" in services.prompts[-1]      # 中断前那句不并被顶掉
    keeper.current().close()


def test_retry_with_nothing_to_continue_says_so(monkeypatch, tmp_path) -> None:
    """新会话里空着 ``/retry``：明说没得接，别拿空历史去问模型。"""
    keeper = SessionStore(root=tmp_path)
    provider = ScriptedProvider([])              # 一旦发请求就会炸

    services, _ = _run(monkeypatch, provider, ["/retry", "/exit"], keeper)

    assert services.messages == ["这一轮已经结束，没有可以接着执行的内容"]
    assert provider.requests == []
    keeper.current().close()


def test_rm_deletes_this_session_and_leaves(monkeypatch, tmp_path) -> None:
    """``/rm`` 只删本会话；一条都不剩时才退出，不能顺手再开一条空的。

    删掉的正是「不想留」的那条，回来却又多出一条新的，等于没删。
    """
    keeper = SessionStore(root=tmp_path)
    provider = ScriptedProvider([_text("答复")])

    _, result = _run(monkeypatch, provider, ["任务一", "/rm", "y"], keeper)

    # handler="default" 就是「用户主动退出」，跟 /exit 一样。
    assert result.handler == "default"
    assert keeper.list(include_archived=True) == []
    assert keeper.current_id() is None           # 当前会话指针也跟着清掉
    left = [path.name for path in tmp_path.iterdir()]
    assert left == ["index.json"]                 # 只剩索引，没有空会话目录


def test_rm_keeps_the_session_and_stays_when_the_answer_is_not_yes(
    monkeypatch, tmp_path
) -> None:
    """默认是不动它：答别的、或干脆回车，都不能把会话删掉，也不能退出。"""
    keeper = SessionStore(root=tmp_path)
    provider = ScriptedProvider([_text("答复")])

    services, _ = _run(monkeypatch, provider, ["任务一", "/rm", "", "/exit"], keeper)

    assert keeper.current_id() is not None            # 会话还在，指针也没掉
    assert len(services.prompts) > 2                  # 答完又回到了提示行
    alive = keeper.list()
    assert len(alive) == 1
    kept = keeper.get(alive[0].id)
    assert any(event.type == "user" for event in kept.events())
    kept.close()


def test_rm_with_an_argument_only_shows_the_usage(monkeypatch, tmp_path) -> None:
    keeper = SessionStore(root=tmp_path)

    services, _ = _run(monkeypatch, ScriptedProvider([]), ["/rm abc", "/exit"], keeper)

    assert keeper.current_id() is not None
    assert services.messages == ["用法：/rm（删除本会话，不需要参数）"]
    keeper.current().close()


def test_archive_moves_the_session_out_of_the_default_list(monkeypatch, tmp_path) -> None:
    """``/archive`` 是移走不是删掉：默认列表里不再出现，``--all`` 还看得到。

    和 ``/rm`` 一样：收起来的那条不再接着用，但也不顺手开一条新的；
    一条都不剩时才退出 agent。
    """
    keeper = SessionStore(root=tmp_path)
    provider = ScriptedProvider([_text("答复")])

    _, result = _run(monkeypatch, provider, ["任务一", "/archive", "y"], keeper)

    assert result.handler == "default"
    assert keeper.list() == []
    archived = keeper.list(include_archived=True)
    assert len(archived) == 1 and archived[0].archived
    assert (tmp_path / "_archive" / archived[0].id / "events.jsonl").is_file()


def test_rm_offers_the_remaining_sessions_instead_of_leaving(
    monkeypatch, tmp_path
) -> None:
    """``/rm`` 删完不退出：把剩下的会话摆出来，挑中的那条接着用。"""
    keeper = SessionStore(root=tmp_path)
    kept = keeper.create("stub", "stub-model", title="留着的那条")
    kept.close()
    doomed = keeper.create("stub", "stub-model", title="要删的那条")
    doomed.close()
    provider = ScriptedProvider([_text("答复")])

    async def choose(services, items, labels, **kwargs):
        services.messages.append(kwargs.get("header", ""))
        return next(meta for meta in items if meta.id == kept.id)

    monkeypatch.setattr(agent_cmd, "pick", choose)
    services, result = _run(
        monkeypatch, provider, ["任务一", "/rm", "y", "/exit"], keeper
    )

    assert [meta.id for meta in keeper.list()] == [kept.id]   # 只删了那一条
    assert keeper.current_id() == kept.id                     # 挑中的成了当前会话
    assert result.handler == "default"                        # 第二条里 /exit 才退出
    assert any("已删除" in text for text in services.messages)


def test_archive_offers_the_remaining_sessions_instead_of_leaving(
    monkeypatch, tmp_path
) -> None:
    """``/archive`` 同样：收起来之后让人挑一条接着用，不退出。"""
    keeper = SessionStore(root=tmp_path)
    kept = keeper.create("stub", "stub-model", title="留着的那条")
    kept.close()
    doomed = keeper.create("stub", "stub-model", title="要收的那条")
    doomed.close()
    provider = ScriptedProvider([_text("答复")])

    async def choose(services, items, labels, **kwargs):
        services.messages.append(kwargs.get("header", ""))
        return next(meta for meta in items if meta.id == kept.id)

    monkeypatch.setattr(agent_cmd, "pick", choose)
    services, result = _run(
        monkeypatch, provider, ["任务一", "/archive", "y", "/exit"], keeper
    )

    assert [meta.id for meta in keeper.list()] == [kept.id]      # 只收起了那一条
    archived = keeper.list(include_archived=True)
    assert [meta.id for meta in archived if meta.archived] == [doomed.id]
    assert keeper.current_id() == kept.id
    assert result.handler == "default"
    assert any("已归档" in text for text in services.messages)


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
    assert any(text.endswith("<task>\n任务一\n</task>") for text in user_texts)
    assert INTERRUPT_MARKER in user_texts
    session.close()


def test_what_was_sent_is_what_replays(monkeypatch, tmp_path) -> None:
    """送出去的那条消息，重放时逐字相同：缓存的前缀才连得上。"""
    first = SessionStore(root=tmp_path)
    provider = ScriptedProvider([_text("答复一")])
    _run(monkeypatch, provider, ["/exit"], first, args="任务一")
    sent = provider.requests[0][-1].content or ""
    first.current().close()

    reopened = SessionStore(root=tmp_path)
    provider = ScriptedProvider([_text("答复二")])
    _run(monkeypatch, provider, ["/exit"], reopened, args="任务二")

    replayed = provider.requests[0]
    assert replayed[1].role == "user"
    assert replayed[1].content == sent
    assert sent.startswith("<terminal_state>\nscreen\n</terminal_state>\n\n<task>\n")
    assert sent.endswith("<task>\n任务一\n</task>")


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


# ---- 参数、/status、/title ----


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


def test_status_pages_the_window(monkeypatch, tmp_path) -> None:
    keeper = SessionStore(root=tmp_path)
    provider = ScriptedProvider([_text("答复"), _text("答复"), _text("答复")])
    _run(monkeypatch, provider, ["任务二", "任务三", "/exit"], keeper, args="任务一")

    services = FakeServices([])
    monkeypatch.setattr(agent_cmd, "store", lambda: keeper)
    monkeypatch.setattr(agent_cmd, "default_catalog", lambda: FakeCatalog(ScriptedProvider([_text("答复")])))
    services._answers = ["/s", "/exit"]
    # 翻看用 read_key：方向键/退出都要走解析后的按键
    services._key = Key(char="q", raw=b"q")
    asyncio.run(agent_cmd.cmd_agent(services, None))

    screen = services.painted[-1].split("\n")
    body = "\n".join(screen)
    # 三条会话都翻得到：提示只留最近两条，pager 要给出全部
    assert "(agent) 任务一" in body
    assert "(agent) 任务三" in body
    assert "(agent-reply) " in body
    assert "窗口" not in body
    # 覆盖层恰好一屏：正文 + 末行状态
    assert len(screen) == services.screen.rows
    assert "tok" in screen[-1]


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

    # 不再直接退出：先说明被占用，再把会话列表摆出来让人换一条。
    assert "占用" in services.messages[-1]
    assert services.painted
    assert "!" + " " + session.id in services.painted[-1]
    other.close()


def test_a_busy_session_lets_you_pick_another_one(monkeypatch, tmp_path) -> None:
    keeper = SessionStore(root=tmp_path)
    busy = keeper.create("stub", "m")
    busy.close()
    spare = keeper.create("stub", "m")
    spare.close()
    keeper.set_current(busy.id)
    holder = SessionStore(root=tmp_path).open(busy.id)
    wanted = [meta for meta in keeper.list() if meta.id == spare.id][0]

    seen = {}

    async def choose(services, store_, current="", busy="", footer="", header=""):
        seen["header"] = header
        return wanted

    services = FakeServices([])
    monkeypatch.setattr(agent_cmd, "store", lambda: SessionStore(root=tmp_path))
    monkeypatch.setattr(
        agent_cmd, "default_catalog", lambda: FakeCatalog(ScriptedProvider([]))
    )
    monkeypatch.setattr(agent_cmd, "_choose_session", choose)
    asyncio.run(agent_cmd.cmd_agent(services, None))

    # 选中另一条后直接进去了，被占用的那条还归对方。
    assert SessionStore(root=tmp_path).current_id() == spare.id
    assert seen["header"] == "会话 {} 正被另一个进程占用，请另选一条：".format(busy.id)
    assert "占用" in services.messages[-1]
    holder.close()


def test_a_wrong_resume_spec_falls_back_to_the_picker(monkeypatch, tmp_path) -> None:
    keeper = SessionStore(root=tmp_path)
    spare = keeper.create("stub", "m")
    spare.close()

    services = FakeServices([])
    monkeypatch.setattr(agent_cmd, "store", lambda: SessionStore(root=tmp_path))
    monkeypatch.setattr(
        agent_cmd, "default_catalog", lambda: FakeCatalog(ScriptedProvider([]))
    )
    asyncio.run(agent_cmd.cmd_agent(services, "--resume nope"))

    # 指错的 --resume 不退出：报出原因后同样摆列表，取消选就结束。
    assert "nope" in services.messages[-1] or "会话" in services.messages[-1]
    assert services.painted
    assert spare.id in services.painted[-1]


def test_a_missing_provider_also_offers_the_picker(monkeypatch, tmp_path) -> None:
    keeper = SessionStore(root=tmp_path)
    session = keeper.create("stub", "m")
    session.close()

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
    monkeypatch.setattr(agent_cmd, "store", lambda: SessionStore(root=tmp_path))
    monkeypatch.setattr(agent_cmd, "default_catalog", lambda: Missing())
    asyncio.run(agent_cmd.cmd_agent(services, "--resume " + session.id))

    assert "gone" in services.messages[-1]
    assert services.painted
    assert session.id in services.painted[-1]


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


# ---- sessions / fork / rm ----


def test_sessions_command_lists_sessions(monkeypatch, tmp_path) -> None:
    keeper = SessionStore(root=tmp_path)
    session = keeper.create("stub", "m")
    session.append_user("任务")
    session.append_assistant("答复")
    session.close()
    monkeypatch.setattr(sessions_cmd, "store", lambda: keeper)

    services = FakeServices([])
    asyncio.run(sessions_cmd.cmd_sessions(services, None))

    assert session.id in services.messages[-1]


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
