"""工具结果的事实层：文本照旧，另带一份 ``data``。

文本仍是模型看到的那份、也是事件里原样保留的那份；``data`` 只放从文本里
不好可靠取回来的字段（光标、行范围、退出码）。它要穿过工具箱的中间件，
最后落到 ``tool/result`` 事件的 ``meta.raw``。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from lls.ai.tools import Observation, Toolbox, builtin_tools
from lls.ai.tools.files import EDIT_FILE_TOOL, edit_file, read_file


class FakeConsole:
    def __init__(self) -> None:
        self.writes: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.writes.append(bytes(data))

    def close(self) -> None:  # pragma: no cover - 仅满足后端接口
        pass


class FakeScreen:
    columns = 80
    rows = 24
    alternate = False
    host_modes: frozenset[int] = frozenset()

    def __init__(self) -> None:
        self.cursor = SimpleNamespace(x=2, y=4, hidden=False)
        self.dirty: set[int] = set()
        self.title = ""
        self.cursor_style = None
        self.clipboard = None

    def text(self, **kwargs) -> str:
        return "hello\nworld"

    def clear_dirty(self) -> None:
        self.dirty.clear()

    def row_cells(self, row: int) -> list:
        return []

    def resize(self, rows: int, columns: int) -> None:  # pragma: no cover
        pass


class FakePort:
    def __init__(self) -> None:
        self.screen = FakeScreen()
        self.written: list[tuple[bytes, bool]] = []
        self.resized: list[tuple[int, int]] = []

    def write(self, data: bytes, *, enter: bool = False) -> None:
        self.written.append((data, enter))

    def flush_model(self) -> None:
        pass

    async def wait(self, timeout: float) -> None:
        await asyncio.sleep(0)

    def resize(self, rows: int, columns: int) -> tuple[int, int]:
        self.resized.append((rows, columns))
        return rows, columns


def _entries(port: FakePort) -> dict:
    return {item.tool.name: item for item in builtin_tools(port, port.wait)}


# ---- 载体本身 ----


def test_an_observation_is_still_a_string() -> None:
    obs = Observation("已写入 3 字节", {"bytes": 3})

    assert obs == "已写入 3 字节"
    assert isinstance(obs, str)
    assert obs.data == {"bytes": 3}


def test_middleware_can_rewrite_text_without_losing_the_facts() -> None:
    box = Toolbox()
    box.add(
        EDIT_FILE_TOOL,
        lambda args: asyncio.sleep(0, result=Observation("ok", {"path": "x", "n": 1})),
    )

    async def shout(name, arguments, call):
        return (await call()) + "!"

    box.add_middleware(shout)

    result = asyncio.run(list(box)[0].run({}))

    assert str(result) == "ok!"
    assert result.data == {"path": "x", "n": 1}


def test_a_middleware_that_refuses_reports_no_facts() -> None:
    box = Toolbox()
    box.add(
        EDIT_FILE_TOOL,
        lambda args: asyncio.sleep(0, result=Observation("ok", {"n": 1})),
    )

    async def refuse(name, arguments, call):
        return "被拒绝"

    box.add_middleware(refuse)

    result = asyncio.run(list(box)[0].run({}))

    assert result == "被拒绝"
    assert getattr(result, "data", {}) == {}


# ---- 各工具报告的事实 ----


def test_read_terminal_reports_geometry_and_status() -> None:
    port = FakePort()

    result = asyncio.run(
        _entries(port)["read_terminal"].run({"lines": 10, "wait_seconds": 0})
    )

    assert result.data == {
        "status": "quiet",
        "rows": 24,
        "cols": 80,
        "cursor": [5, 3],
        "buffer": "main",
    }


def test_read_file_reports_the_range_it_returned(tmp_path) -> None:
    target = tmp_path / "x.txt"
    target.write_text("a\nb\nc\n", encoding="utf-8")

    result = asyncio.run(
        read_file({"path": str(target), "start_line": 2, "max_lines": 2})
    )

    assert result.data["start"] == 2
    assert result.data["end"] == 3
    assert result.data["total"] == 3
    assert result.data["truncated"] is False
    assert result.data["lines"] == 2
    assert "screen" not in result.data and "body" not in result.data


def test_edit_file_reports_how_many_it_replaced(tmp_path) -> None:
    target = tmp_path / "x.txt"
    target.write_text("a a\n", encoding="utf-8")

    result = asyncio.run(
        edit_file(
            {
                "path": str(target),
                "old_string": "a",
                "new_string": "b",
                "replace_all": True,
            }
        )
    )

    assert result.data["replacements"] == 2
    assert result.data["path"] == str(target)


# ---- 落到事件里 ----


def test_the_agent_puts_the_facts_on_the_tool_message(monkeypatch) -> None:
    from lls.ai import agent as agent_mod
    from lls.ai.types import Message, ToolCall

    box = Toolbox()
    box.add(
        EDIT_FILE_TOOL,
        lambda args: asyncio.sleep(0, result=Observation("ok", {"n": 7})),
    )
    captured: list[Message] = []
    agent = agent_mod.Agent(
        None,  # type: ignore[arg-type]
        "m",
        system="s",
        tools=box,
        on_message=lambda message, usage: captured.append(message),
    )
    rounds = [("", [ToolCall("c1", "edit_file", "{}")]), ("done", [])]

    async def fake_round(step, messages, tools):
        return rounds.pop(0)

    monkeypatch.setattr(agent, "_round", fake_round)

    asyncio.run(agent.run("task"))

    tool_messages = [m for m in captured if m.role == "tool"]
    assert tool_messages[0].content == "ok"
    assert tool_messages[0].meta == {"n": 7}
    assert "meta" not in tool_messages[0].to_payload()  # 不进请求体


def test_the_session_records_the_facts_as_meta_raw(tmp_path) -> None:
    from lls.ai import turn as turn_mod
    from lls.ai.sessions import SessionStore
    from lls.ai.types import Message

    keeper = SessionStore(root=tmp_path)
    session = keeper.create("p", "m")
    state = turn_mod.Turn(
        services=FakePort(),
        view=None,
        provider=None,
        model="model",
        session=session,
        tools=(),
    )
    state._calls["c1"] = "bash"

    state._on_message(
        Message(
            "tool", "（退出码 0，用时 0.10s）", tool_call_id="c1",
            meta={"exit": 0},
        ),
        None,
    )

    results = [e for e in session.events() if e.type == "tool/result"]
    raw = results[-1].data["meta"]["raw"]
    assert raw == {"tool": "bash", "exit": 0}
    assert results[-1].data["message"]["content"][0]["text"].startswith("（退出码 0")
    session.close()

