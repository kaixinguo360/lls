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
    RepeatReminder,
    ScreenView,
    Shell,
    Toolbox,
    builtin_tools,
)

from lls.ai.types import Tool


class FakePort:
    """四方法的最小终端：满足 ``TerminalPort``，没有引擎。"""

    def __init__(self, text: str = "line") -> None:
        self.screen = SimpleNamespace(text=lambda limit=0: text)
        self.written = []
        self.flushed = 0

    def write(self, data: bytes, *, enter: bool = False) -> None:
        self.written.append((data, enter))

    def flush_model(self) -> None:
        self.flushed += 1

    async def wait(self, timeout: float) -> None:
        await asyncio.sleep(0)


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
        "read_file",
        "read_terminal",
        "send_keys",
        "send_raw",
        "send_text",
        "wait_for",
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


def test_the_shell_keeps_its_own_cwd(tmp_path) -> None:
    """cwd 属于某个 shell 实例，不再是模块级全局。"""
    shell = Shell(str(tmp_path))
    (tmp_path / "a").mkdir()

    assert shell.target("a") == os.path.join(str(tmp_path), "a")
    assert shell.target("") == str(tmp_path)
    assert shell.target("~/x").startswith(os.path.expanduser("~"))
