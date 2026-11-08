"""工具箱的接缝：装配、中间件、端口。

这层是「工具与 agent 命令解耦」的落点，所以测的是接缝而不是某个工具的细节
（工具行为在 ``test_agent_session.py`` 里走真实装配路径测）。

三个约定：

1. ``Toolbox`` 只认 :class:`lls.ai.AgentTool`，加工具不改 Agent、不改命令；
2. 中间件套在**所有**工具外面，能看能改结果，也能不调用下一层；
3. 工具依赖的是窄端口（``TerminalPort`` / ``Shell``），拿个假端口就能跑。
"""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

from lls.ai import AgentTool
from lls.ai.tools import (
    ReadTerminalSettings,
    RepeatReminder,
    ScreenView,
    Shell,
    Toolbox,
    builtin_tools,
)
from lls.ai.types import Tool


class FakePort:
    """最小终端：满足 ``TerminalPort``，没有引擎。"""

    def __init__(self, text: str = "line") -> None:
        self.screen = SimpleNamespace(
            text=make_screen_text(text),
            rows=24,
            columns=80,
            cursor=SimpleNamespace(x=0, y=0, hidden=False),
            alternate=False,
            host_modes=frozenset(),
        )
        self.written = []
        self.flushed = 0
        self.resized: list[tuple[int, int]] = []

    def write(self, data: bytes, *, enter: bool = False) -> None:
        self.written.append((data, enter))

    def flush_model(self) -> None:
        self.flushed += 1

    async def wait(self, timeout: float) -> None:
        await asyncio.sleep(0)

    def resize(self, rows: int, columns: int) -> tuple[int, int]:
        self.resized.append((rows, columns))
        return rows, columns


def make_screen_text(text: str):
    """记录读取范围；visible_only 会传 ``include_scrollback=False``。"""
    calls: list[dict] = []

    def read(limit=0, include_scrollback=True):
        calls.append({"limit": limit, "include_scrollback": include_scrollback})
        return text

    read.calls = calls
    return read


def _tool(name: str) -> Tool:
    return Tool(
        name=name,
        description="d",
        parameters={"type": "object", "properties": {}, "required": []},
    )


def _run(entry: AgentTool, arguments=None):
    return asyncio.run(entry.run(arguments or {}))


# ---- 装配 ----


def test_builtin_tools_are_strict_and_named() -> None:
    box = builtin_tools(FakePort(), FakePort().wait)

    assert sorted(box.names()) == [
        "bash",
        "edit_file",
        "read_file",
        "read_terminal",
        "resize_terminal",
        "send_keys",
        "send_raw",
        "send_text",
        "wait_for",
        "wait_for_change",
        "write_file",
    ]
    assert all(entry.tool.strict for entry in box)


def test_a_tool_can_be_added_without_touching_the_agent() -> None:
    box = Toolbox()
    box.add(_tool("hello"), lambda args: asyncio.sleep(0, result="hi"))

    entry = list(box)[0]
    assert entry.tool.name == "hello"
    assert asyncio.run(entry.run({})) == "hi"


# ---- 中间件 ----


def test_middleware_sees_every_tool_and_can_reorder_the_chain() -> None:
    seen = []

    def outer(name, arguments, call):
        async def run():
            seen.append(("before", name))
            text = await call()
            seen.append(("after", name))
            return text + "!"

        return run()

    box = Toolbox()
    box.add(_tool("a"), lambda args: asyncio.sleep(0, result="a"))
    box.add(_tool("b"), lambda args: asyncio.sleep(0, result="b"))
    box.add_middleware(outer)

    assert [_run(entry) for entry in box] == ["a!", "b!"]
    assert seen == [("before", "a"), ("after", "a"), ("before", "b"), ("after", "b")]


def test_middleware_may_refuse_a_call_without_reaching_the_tool() -> None:
    """拒绝一次调用（例如用户没批准写文件）用不着改工具本身。"""

    def refuse(name, arguments, call):
        async def run():
            return "被拒绝"

        return run()

    called = []
    box = Toolbox()
    box.add(_tool("write_file"), lambda args: called.append(args) or asyncio.sleep(0))
    box.add_middleware(refuse)

    assert _run(list(box)[0]) == "被拒绝"
    assert called == []


def test_repeat_reminder_only_speaks_after_the_third_identical_call() -> None:
    box = Toolbox()
    box.add(_tool("read_terminal"), lambda args: asyncio.sleep(0, result="r"))
    box.add_middleware(RepeatReminder())
    entry = list(box)[0]

    assert "提醒" not in _run(entry, {"lines": 10})
    assert "提醒" not in _run(entry, {"lines": 10})
    assert "提醒" in _run(entry, {"lines": 10})
    assert "提醒" not in _run(entry, {"lines": 20})


# ---- 端口 ----


def test_the_shared_view_is_the_place_that_remembers_what_was_read() -> None:
    """宿主（会话）拿同一份 view 拍快照，模型第一次读屏就不重复读。"""
    port = FakePort("hello")
    view = ScreenView(port, port.wait)

    view.mark("hello")
    box = builtin_tools(port, port.wait, view=view)
    entry = {item.tool.name: item for item in box}["read_terminal"]

    assert "与上次读取相同" in _run(entry, {"lines": 10, "wait_seconds": 0})


def test_tool_setting_overrides_the_global_default() -> None:
    class _Options:
        settings = {"visible_only": False}

    class _Unset:
        settings = None

    assert ReadTerminalSettings.from_options(
        _Options(), default=True
    ).visible_only is False
    assert ReadTerminalSettings.from_options(
        _Unset(), default=True
    ).visible_only is True


def _visible_settings() -> dict[str, ReadTerminalSettings]:
    return {
        name: ReadTerminalSettings(visible_only=True)
        for name in ("read_terminal", "wait_for", "wait_for_change")
    }


def test_visible_only_hides_lines_and_reads_the_visible_screen() -> None:
    port = FakePort("visible")
    box = builtin_tools(port, port.wait, read_settings=_visible_settings())
    tools = {item.tool.name: item for item in box}

    for name in ("read_terminal", "wait_for", "wait_for_change"):
        parameters = tools[name].tool.parameters
        assert "lines" not in parameters["properties"]
        assert "lines" not in parameters["required"]

    asyncio.run(tools["read_terminal"].run({"wait_seconds": 0}))

    assert port.screen.text.calls == [{"limit": 24, "include_scrollback": False}]


def test_visible_only_is_configured_per_tool() -> None:
    """只开 read_terminal 时，等画面的两个工具仍然是原来的读法。"""
    port = FakePort("visible")
    box = builtin_tools(
        port,
        port.wait,
        read_settings={"read_terminal": ReadTerminalSettings(visible_only=True)},
    )
    tools = {item.tool.name: item for item in box}

    assert "lines" not in tools["read_terminal"].tool.parameters["properties"]
    assert "lines" in tools["wait_for"].tool.parameters["properties"]
    assert "lines" in tools["wait_for_change"].tool.parameters["properties"]


def test_visible_only_prompt_matches_the_schema() -> None:
    from lls.ai import prompt

    port = FakePort("visible")
    box = builtin_tools(port, port.wait, read_settings=_visible_settings())

    text = prompt.build(box, display="interactive")

    assert "read_terminal(wait_seconds)" in text
    assert "wait_for(pattern, timeout, include_existing)" in text
    assert "wait_for_change(timeout)" in text
    assert "read_terminal(lines" not in text


def test_the_shell_keeps_its_own_cwd(tmp_path) -> None:
    """cwd 属于某个 shell 实例，不再是模块级全局。"""
    shell = Shell(str(tmp_path))
    (tmp_path / "a").mkdir()

    assert shell.target("a") == os.path.join(str(tmp_path), "a")
    assert shell.target("") == str(tmp_path)
    assert shell.target("~/x").startswith(os.path.expanduser("~"))
