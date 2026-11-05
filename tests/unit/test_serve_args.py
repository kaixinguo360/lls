"""``lls serve`` 的参数解析：只认四个开关和 ``--`` 之后的命令。"""

from __future__ import annotations

import os

from lls.headless import _parse


def test_task_and_command() -> None:
    options = _parse(["--task", "整理一下", "--", "zsh", "-l"])

    assert options.task == "整理一下"
    assert options.command == ["zsh", "-l"]
    assert options.headless is False
    assert options.stay is False


def test_default_command_is_the_shell() -> None:
    options = _parse(["--task", "x"])

    assert options.command == [os.environ.get("SHELL") or "/bin/sh"]


def test_stay_and_headless_are_independent() -> None:
    options = _parse(["--headless", "--stay"])

    assert options.headless is True
    assert options.stay is True


def test_missing_task_is_empty_not_an_error() -> None:
    options = _parse([])

    assert options.task == ""
    assert options.session == ""


def test_session_wins_over_new() -> None:
    options = _parse(["--new", "--session", "abc123"])

    assert options.session == "abc123"


def test_whitespace_task_is_treated_as_missing() -> None:
    options = _parse(["--task", "   "])

    assert options.task == ""
