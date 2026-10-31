"""``tools`` 命令：全局层开关。

命令只做三件事：列出默认工具、把当前全局状态交给开关界面、把结果写回
全局策略。界面本身的按键语义在 ``test_tool_picker.py`` 里测。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from lls.ai.tool_options import ToolOptions
from lls.ai.tools.policy import global_policy, reset_global_policy
from lls.commands.builtin import tools as tools_cmd
from lls.commands.spec import get_command


def _key(control: str = "", char: str = "", raw: bytes = b"") -> SimpleNamespace:
    return SimpleNamespace(control=control, char=char, raw=raw)


class FakeServices:
    def __init__(self, keys=()) -> None:
        self._keys = list(keys)
        self.screen = SimpleNamespace(rows=12, columns=60)
        self.messages: list[str] = []

    async def read_key(self):
        if not self._keys:
            await asyncio.sleep(0.5)
            return None
        return self._keys.pop(0)

    async def show_message(self, text: str) -> None:
        self.messages.append(text)

    def draw_overlay(self, text, cursor=None):
        return (0, len(text.splitlines()))

    def set_overlay_source(self, source=None) -> None:
        pass

    def write(self, data: bytes, *, enter: bool = False) -> None:
        pass

    def flush_model(self) -> None:
        pass

    async def wait(self, timeout: float) -> None:
        await asyncio.sleep(0)


def test_the_tools_command_is_registered() -> None:
    spec = get_command("tools")

    assert spec is not None
    assert spec.fn is tools_cmd.cmd_tools


def test_switching_a_tool_off_updates_the_global_policy() -> None:
    reset_global_policy()
    # 第二项是 wait_for：先下移一行，空格，回车保存。
    services = FakeServices([_key("down"), _key(char=" "), _key("enter")])

    asyncio.run(tools_cmd.cmd_tools(services, None))

    assert global_policy().enabled("wait_for") is False
    assert global_policy().enabled("read_terminal") is True
    assert any("wait_for" in message for message in services.messages)
    reset_global_policy()


def test_cancelling_leaves_the_global_policy_alone() -> None:
    reset_global_policy()
    services = FakeServices([_key(char=" "), _key(char="q")])

    asyncio.run(tools_cmd.cmd_tools(services, None))

    assert global_policy().disabled() == []
    reset_global_policy()


def test_tool_switches_carry_the_global_state() -> None:
    reset_global_policy({"write_file": ToolOptions(enabled=False)})

    items = tools_cmd.tool_switches(["write_file", "bash"])

    assert items[0].enabled is False
    assert items[0].source == "global"
    assert items[1].enabled is True
    reset_global_policy()


def test_tool_names_keep_the_request_order() -> None:
    services = FakeServices()

    names = tools_cmd.tool_names(services)

    assert names[:2] == ["read_terminal", "wait_for"]
    assert names[-1] == "bash"
