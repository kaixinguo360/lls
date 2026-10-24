"""行编辑器单元测试（钉死 legacy read_line 的编辑语义）。"""

from __future__ import annotations

from lls.edit import LineEditor, display_width, render_prompt_line


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
    assert display_width("中") == 2
    assert display_width("ab中") == 4


def test_render_prompt_line_highlight() -> None:
    e = _ed()
    for c in "ab":
        e.insert(c)
    display, col = render_prompt_line("> ", e, columns=80)
    # 光标在行尾（x=2），高亮一个空格
    assert "\x1b[7m \x1b[0m" in display
    # 列号 = prompt(2) + line(2) = 4
    assert col == 4


def test_render_prompt_line_mid_cursor() -> None:
    e = _ed()
    for c in "abc":
        e.insert(c)
    e.left()  # 光标在 b 处（x=2）
    display, col = render_prompt_line("> ", e, columns=80)
    assert "\x1b[7mc\x1b[0m" in display  # 高亮 c（光标处字符）
    assert col == 4  # prompt(2) + "ab"(2)
