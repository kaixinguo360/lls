"""系统提示词按工具清单与运行场景生成。

两件事必须成立：禁用的工具不出现（否则模型会去调一个不存在的工具），
场景不同则措辞不同（有任务 / 待命、有人看 / 没人看）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from lls.ai import prompt


def _tools(*names: str) -> list[SimpleNamespace]:
    return [SimpleNamespace(name=name) for name in names]


def test_only_assembled_tools_are_described() -> None:
    text = prompt.build(_tools("read_terminal", "send_text"), display="interactive")

    assert "read_terminal(lines, wait_seconds)" in text
    assert "send_text(text, enter)" in text
    assert "bash(command, cwd, timeout)" not in text


def test_wait_for_change_is_described_when_present() -> None:
    text = prompt.build(_tools("read_terminal", "wait_for_change"), display="headless")

    assert "wait_for_change(timeout, lines)" in text


def test_interactive_display_matches_the_reply_budget() -> None:
    text = prompt.build(
        _tools("read_terminal"), display="interactive", reply_lines=7, log_entries=3
    )

    assert "最多 7 行" in text
    assert "最近 3 轮" in text
    assert "终端画面就是细节所在" in text


def test_headless_writes_the_answer_to_stdout() -> None:
    text = prompt.build(_tools("read_terminal"), display="headless")

    assert "stdout" in text
    assert "终端会话提示里" not in text


def test_standby_tells_the_model_not_to_invent_work() -> None:
    text = prompt.build(_tools("wait_for_change"), display="headless", mode="standby")

    assert "没有任务" in text
    assert "不要发明目标" in text
    assert "输出文本就等于结束这次运行" in text


def test_task_mode_ends_with_the_answer() -> None:
    text = prompt.build(_tools("read_terminal"), display="interactive", mode="task")

    assert "直接给出文本答复，不要再调用工具" in text


def test_unknown_scene_is_an_error() -> None:
    with pytest.raises(ValueError):
        prompt.build(_tools("read_terminal"), display="tty", mode="task")


def test_no_tools_still_produces_a_prompt() -> None:
    """工具全被关掉是合法状态：照常拼提示词，模型只能直接答复。"""
    text = prompt.build([], display="interactive")

    assert "没有可用的工具" in text


def test_every_builtin_tool_has_a_description() -> None:
    """新增工具时必须同时补一条说明，否则提示词里会漏掉它。"""
    from lls.ai.tools import builtin_tools

    box = builtin_tools(_Port(), _Port().wait)
    missing = [entry.tool.name for entry in box if entry.tool.name not in prompt._TOOL_HELP]

    assert missing == []


class _Port:
    """够 ``builtin_tools`` 装配就行，不跑任何工具。"""

    def __init__(self) -> None:
        self.screen = SimpleNamespace(
            text=lambda limit=0: "",
            rows=24,
            columns=80,
            cursor=SimpleNamespace(x=0, y=0, hidden=False),
            alternate=False,
            host_modes=frozenset(),
        )

    def write(self, data: bytes, *, enter: bool = False) -> None:
        pass

    def flush_model(self) -> None:
        pass

    async def wait(self, timeout: float) -> None:
        pass

    def resize(self, rows: int, columns: int) -> tuple[int, int]:
        return rows, columns
