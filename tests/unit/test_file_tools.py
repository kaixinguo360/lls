"""文件工具：按行读（带行号、远偏移也读得到）与按字面编辑。

``read_file`` 以前只读文件头 800KB 再切片，``start_line`` 超过那里就谎报
「之后没有内容」；现在逐行跳过，十万行的日志从第 90000 行读和从第 1 行读
一样快。``edit_file`` 默认要求恰好一处匹配：零处或多处都报错，让模型自己
补上下文，而不是猜一处改掉。
"""

from __future__ import annotations

import asyncio

import pytest

from lls.ai.agent import AgentTool
from lls.ai.tools.files import (
    EDIT_FILE_TOOL,
    READ_FILE_TOOL,
    WRITE_FILE_TOOL,
    edit_file,
    read_file,
    write_file,
)

_TOOLS = {
    "read_file": (READ_FILE_TOOL, read_file),
    "write_file": (WRITE_FILE_TOOL, write_file),
    "edit_file": (EDIT_FILE_TOOL, edit_file),
}


def _run(name: str, **args) -> str:
    tool, fn = _TOOLS[name]
    return asyncio.run(AgentTool(tool, fn).run(args))


# ---- read_file ----


def test_read_file_numbers_lines_and_says_what_comes_next(tmp_path) -> None:
    target = tmp_path / "x.txt"
    target.write_text("a\nb\nc\n", encoding="utf-8")

    out = _run("read_file", path=str(target), start_line=1, max_lines=2)

    assert "1: a" in out and "2: b" in out
    assert "第 1-2 行，后面还有" in out
    assert "（用 start_line=3 接着读）" in out


def test_read_file_marks_the_end_of_the_file(tmp_path) -> None:
    target = tmp_path / "x.txt"
    target.write_text("a\nb\n", encoding="utf-8")

    out = _run("read_file", path=str(target), start_line=1, max_lines=10)

    assert "第 1-2 行，共 2 行" in out
    assert "接着读" not in out


def test_read_file_reaches_a_line_far_past_the_first_chunk(tmp_path) -> None:
    """旧实现只读文件头 800KB：这里第 4001 行必须还能读到。"""
    target = tmp_path / "big.log"
    with open(target, "w", encoding="utf-8") as handle:
        for index in range(1, 5001):
            handle.write(f"line {index} " + "x" * 200 + "\n")

    out = _run("read_file", path=str(target), start_line=4001, max_lines=2)

    assert "4001: line 4001" in out
    assert "4002: line 4002" in out
    assert "start_line=4003" in out


def test_read_file_says_so_past_the_end(tmp_path) -> None:
    target = tmp_path / "x.txt"
    target.write_text("a\n", encoding="utf-8")

    out = _run("read_file", path=str(target), start_line=9, max_lines=5)

    assert "共 1 行" in out
    assert "第 9 行之后没有内容" in out


def test_read_file_still_rejects_binary(tmp_path) -> None:
    target = tmp_path / "b.bin"
    target.write_bytes(b"a\x00b")

    with pytest.raises(ValueError, match="binary"):
        _run("read_file", path=str(target), start_line=1, max_lines=5)


# ---- edit_file ----


def test_edit_replaces_one_match_and_keeps_the_rest(tmp_path) -> None:
    target = tmp_path / "x.txt"
    target.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")

    out = _run(
        "edit_file",
        path=str(target),
        old_string="beta",
        new_string="BETA",
        replace_all=False,
    )

    assert target.read_text(encoding="utf-8") == "alpha\nBETA\ngamma\n"
    assert "已替换 1 处" in out


def test_edit_replaces_all_when_asked(tmp_path) -> None:
    target = tmp_path / "x.txt"
    target.write_text("a a a\n", encoding="utf-8")

    out = _run(
        "edit_file",
        path=str(target),
        old_string="a",
        new_string="b",
        replace_all=True,
    )

    assert target.read_text(encoding="utf-8") == "b b b\n"
    assert "已替换 3 处" in out


def test_edit_refuses_an_ambiguous_match(tmp_path) -> None:
    target = tmp_path / "x.txt"
    target.write_text("a\na\n", encoding="utf-8")

    with pytest.raises(ValueError, match="appears 2 times"):
        _run(
            "edit_file",
            path=str(target),
            old_string="a",
            new_string="b",
            replace_all=False,
        )

    assert target.read_text(encoding="utf-8") == "a\na\n"


def test_edit_refuses_a_missing_match_and_says_why(tmp_path) -> None:
    target = tmp_path / "x.txt"
    target.write_text("a\n", encoding="utf-8")

    with pytest.raises(ValueError, match="does not appear"):
        _run(
            "edit_file",
            path=str(target),
            old_string="zzz",
            new_string="b",
            replace_all=False,
        )


def test_edit_refuses_a_no_op_and_a_missing_file(tmp_path) -> None:
    target = tmp_path / "x.txt"
    target.write_text("a\n", encoding="utf-8")

    with pytest.raises(ValueError, match="identical"):
        _run(
            "edit_file",
            path=str(target),
            old_string="a",
            new_string="a",
            replace_all=False,
        )
    with pytest.raises(ValueError, match="cannot read"):
        _run(
            "edit_file",
            path=str(tmp_path / "nope.txt"),
            old_string="a",
            new_string="b",
            replace_all=False,
        )


def test_edit_refuses_non_utf8_instead_of_corrupting_it(tmp_path) -> None:
    target = tmp_path / "latin1.txt"
    target.write_bytes(b"caf\xe9\n")

    with pytest.raises(ValueError, match="not valid UTF-8"):
        _run(
            "edit_file",
            path=str(target),
            old_string="caf",
            new_string="CAF",
            replace_all=False,
        )

    assert target.read_bytes() == b"caf\xe9\n"
