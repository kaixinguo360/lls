"""``lls serve`` 的端到端接线：真 pty + 桩 provider，不联网。

验的是「装配」而不是模型能力：会话建出来、引擎在无宿主模式下跑起来、
一轮结束后自己收尾、退出码跟着结果走。
"""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

import pytest

from lls import headless
from lls.ai import Chunk, ChunkKind
from lls.ai.sessions import SessionStore

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="需要真 pty（Windows 上没有 pty/termios）"
)


class _Provider:
    id = "stub"

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.requests: list = []

    async def chat(self, model, messages, *, tools=None, **params):
        self.requests.append(list(messages))
        if self.fail:
            raise RuntimeError("boom")
        yield Chunk(ChunkKind.TEXT, "做完了")
        yield Chunk(ChunkKind.DONE)


class _Catalog:
    def __init__(self, provider) -> None:
        self._provider = provider

    def resolve_spec(self, spec=None):
        return "stub", "stub-model"

    def get(self, provider_id):
        return self._provider

    def provider_config(self, provider_id):
        return SimpleNamespace(context_window=128_000)

    def context_limit(self):
        return 0

    def tools(self):
        return {}


def _no_recording():
    return SimpleNamespace(recording=SimpleNamespace(enabled=False))


def _serve(tmp_path, monkeypatch, provider, argv):
    keeper = SessionStore(root=tmp_path / "sessions")
    monkeypatch.setattr(headless, "store", lambda: keeper)
    monkeypatch.setattr(headless, "default_catalog", lambda: _Catalog(provider))
    monkeypatch.setattr(headless, "load_config", _no_recording)
    options = headless._parse(argv)

    async def run():
        return await asyncio.wait_for(headless._run(options), 30)

    return asyncio.run(run()), keeper


def test_one_task_round_trip(tmp_path, monkeypatch) -> None:
    provider = _Provider()

    code, keeper = _serve(
        tmp_path,
        monkeypatch,
        provider,
        ["--task", "打个招呼", "--headless", "--", "sh", "-c", "echo hi; sleep 5"],
    )

    assert code == 0
    assert provider.requests, "模型没有被请求过"
    session = keeper.open()
    assert session is not None
    text = "\n".join(message.content for message in session.history())
    assert "打个招呼" in text


def test_provider_failure_exits_nonzero(tmp_path, monkeypatch) -> None:
    provider = _Provider(fail=True)

    code, _ = _serve(
        tmp_path,
        monkeypatch,
        provider,
        ["--task", "x", "--headless", "--", "sh", "-c", "sleep 5"],
    )

    assert code == 1


def test_existing_session_is_reused(tmp_path, monkeypatch) -> None:
    keeper = SessionStore(root=tmp_path / "sessions")
    first = keeper.create("stub", "stub-model", title="旧会话")
    first.close()

    provider = _Provider()
    code, keeper = _serve(
        tmp_path,
        monkeypatch,
        provider,
        ["--task", "接着来", "--session", first.id, "--headless", "--", "sh", "-c", "sleep 5"],
    )

    assert code == 0
    session = keeper.open(first.id)
    assert session is not None
    text = "\n".join(message.content for message in session.history())
    assert "接着来" in text
