"""``/model``：交互选择与直接切换。

选择器由 ``agent_picker.pick`` 负责，这里用假的替换它，只测分支。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from lls.ai.sessions.store import SessionMeta
from lls.commands.builtin import agent as agent_cmd


class FakeServices:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def show_message(self, text: str) -> None:
        self.messages.append(text)


class FakeCatalog:
    def __init__(self, providers) -> None:
        self._providers = providers
        self._context_window = 128_000

    def provider_ids(self):
        return sorted(self._providers)

    def provider_config(self, provider_id):
        return SimpleNamespace(
            models=list(self._providers[provider_id]),
            context_window=self._context_window,
        )

    def context_limit(self):
        return 0

    def resolve_spec(self, spec):
        provider_id, _, model_id = spec.partition("/")
        return provider_id, model_id

    def get(self, provider_id):
        return SimpleNamespace(id=provider_id)


class FakeSession:
    def __init__(self) -> None:
        self.meta = SessionMeta(id="x")
        self.switches: list[str] = []

    def append_model_switch(self, model: str) -> None:
        self.switches.append(model)


class FakeKeeper:
    def __init__(self) -> None:
        self.written = 0

    def write_meta(self, meta) -> None:
        self.written += 1


def _worker(
    catalog=None,
    *,
    provider_id: str = "p",
    model: str = "a",
    typing: bool = False,
    session=None,
    keeper=None,
) -> agent_cmd._Session:
    worker = object.__new__(agent_cmd._Session)
    worker._s = FakeServices()
    worker._provider = SimpleNamespace()
    worker._provider_id = provider_id
    worker._model = model
    worker._catalog = catalog
    worker._budget = 0
    worker._session = session
    worker._keeper = keeper
    worker._typing_active = typing
    return worker


def _fake_pick(monkeypatch, answer):
    calls = []

    async def fake(services, items, labels, **kwargs):
        calls.append({"items": list(items), "labels": list(labels), **kwargs})
        return answer

    monkeypatch.setattr(agent_cmd, "pick", fake)
    return calls


def test_no_argument_opens_the_picker_at_the_current_model(monkeypatch) -> None:
    session, keeper = FakeSession(), FakeKeeper()
    worker = _worker(FakeCatalog({"p": ["a", "b"]}), session=session, keeper=keeper)
    calls = _fake_pick(monkeypatch, "p/b")

    asyncio.run(worker._slash_model(""))

    assert worker._model == "b"
    assert session.meta.model_id == "b"
    assert session.switches == ["p/b"]
    assert keeper.written == 1
    assert calls[0]["items"] == ["p/a", "p/b"]
    assert calls[0]["cursor"] == 0          # 当前模型是 p/a
    assert calls[0]["labels"][0].startswith("* ")
    assert calls[0]["labels"][1].startswith("  ")
    assert any("已切到 p/b" in text for text in worker._s.messages)


def test_cancelling_the_picker_changes_nothing(monkeypatch) -> None:
    session = FakeSession()
    worker = _worker(FakeCatalog({"p": ["a", "b"]}), session=session)
    _fake_pick(monkeypatch, None)

    asyncio.run(worker._slash_model(""))

    assert worker._model == "a"
    assert session.switches == []


def test_without_a_catalog_it_falls_back_to_text() -> None:
    worker = _worker(None)

    asyncio.run(worker._slash_model(""))

    assert any("当前模型" in text for text in worker._s.messages)


def test_without_configured_models_it_falls_back_to_text() -> None:
    worker = _worker(FakeCatalog({"p": []}))

    asyncio.run(worker._slash_model(""))

    assert any("没有可选的模型" in text for text in worker._s.messages)


def test_typing_blocks_the_picker(monkeypatch) -> None:
    calls = _fake_pick(monkeypatch, "p/b")
    worker = _worker(FakeCatalog({"p": ["a", "b"]}), typing=True)

    asyncio.run(worker._slash_model(""))

    assert calls == []
    assert any("执行中" in text for text in worker._s.messages)


def test_an_explicit_spec_skips_the_picker(monkeypatch) -> None:
    calls = _fake_pick(monkeypatch, None)
    worker = _worker(FakeCatalog({"p": ["a", "b"]}))

    asyncio.run(worker._slash_model("p/b"))

    assert calls == []
    assert worker._model == "b"


def test_model_choices_skip_a_broken_provider() -> None:
    catalog = FakeCatalog({"p": ["a"]})

    def broken(provider_id):
        raise RuntimeError("bad config")

    catalog.provider_config = broken
    worker = _worker(catalog)

    assert worker._model_choices() == []
