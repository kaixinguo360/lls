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


def test_prompt_needs_at_least_one_known_tool() -> None:
    with pytest.raises(ValueError):
        prompt.build(_tools("unknown"), display="interactive")
