"""行编辑器的编辑语义。"""

from __future__ import annotations

from lls.edit import LineEditor
from lls.ui import text_width


def _ed() -> LineEditor:
    return LineEditor()


def test_insert_and_cursor() -> None:
    e = _ed()
    for c in "abc":
        e.insert(c)
    assert e.text == "abc"
    assert e.cursor == 3


def test_insert_in_middle() -> None:
    e = _ed()
    for c in "ac":
        e.insert(c)
    e.left()  # 光标移到 a|c
    e.insert("b")
    assert e.text == "abc"
    assert e.cursor == 2


def test_backspace() -> None:
    e = _ed()
    for c in "abc":
        e.insert(c)
    e.backspace()
    assert e.text == "ab"
    assert e.cursor == 2


def test_delete() -> None:
    e = _ed()
    for c in "abc":
        e.insert(c)
    e.home()
    e.delete()
    assert e.text == "bc"
    assert e.cursor == 0


def test_left_right_home_end() -> None:
    e = _ed()
    for c in "abc":
        e.insert(c)
    e.home()
    assert e.cursor == 0
    e.right()
    assert e.cursor == 1
    e.end()
    assert e.cursor == 3
    e.left()
    assert e.cursor == 2


def test_history_up_down() -> None:
    e = _ed()
    e.reset("")
    for c in "one":
        e.insert(c)
    e.submit()  # history: ["one"]
    e.reset("")
    e.insert("t")  # 编辑草稿 "t"
    e.history_up()
    assert e.text == "one"
    e.history_down()
    assert e.text == "t"  # 恢复草稿


def test_history_dedup() -> None:
    e = _ed()
    e.reset("")
    for c in "ls":
        e.insert(c)
    e.submit()
    e.reset("")
    for c in "ls":
        e.insert(c)
    e.submit()
    assert e.history == ["ls"]  # 连续相同不重复


def test_wide_char_cursor_width() -> None:
    assert text_width("中") == 2
    assert text_width("ab中") == 4

# ---- 光标以簇为单位：不切开一个 emoji 或组合字符 ----
#
# 光标落进簇中间时，模型按半截文本量列、终端按完整字形画，两边对不上 ——


def test_backspace_removes_a_whole_cluster() -> None:
    e = _ed()
    for c in "a\u2764\ufe0f":
        e.insert(c)

    e.backspace()

    assert e.text == "a"     # 「心 + VS16」整簇删掉，不留孤零零的 VS16
    assert e.cursor == 1
    e.backspace()
    assert e.text == ""


def test_delete_removes_a_whole_cluster() -> None:
    e = _ed()
    for c in "\U0001f468\u200d\U0001f469\u200d\U0001f467!":
        e.insert(c)
    e.home()

    e.delete()

    assert e.text == "!"     # 一家三口整簇删掉，不散架
    assert e.cursor == 0


def test_left_and_right_step_over_whole_clusters() -> None:
    e = _ed()
    for c in "e\u0301x":
        e.insert(c)          # e + 组合符、x

    e.left()
    assert e.cursor == 2     # 跳过 x，落到「é」之后
    e.left()
    assert e.cursor == 0     # 「é」是一簇，一步跨过去
    e.right()
    assert e.cursor == 2
