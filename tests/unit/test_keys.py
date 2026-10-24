"""按键解析器单元测试。

解析按 ECMA-48 / VT100 的转义序列语法实现，而非枚举常见序列。
回归重点：终端应答（DSR/DA/DECRQM/OSC/…）绝不能漏成可见输入 ——
曾因只认 ``R``/``c``/``n`` 三种终结符，导致 DECRQM 应答 ``ESC[?12;1$y``
的残余 ``y`` 被插入 line 编辑器。
"""

from __future__ import annotations

import pytest

from lls.tty import KeyParser


# ---- 回车：CRLF 是一个键 ----

# 终端、粘贴、远程会话都可能把回车发成 \r\n。若拆成两个 Enter，紧接着的下一次
# 读取会立刻拿到一个空行 —— 表现为「刚进提示就自己回车了一次」，命令凭空退出。


def test_crlf_is_a_single_enter() -> None:
    assert [k.control for k in KeyParser().feed(b"\r\n")] == ["enter"]


def test_crlf_split_across_chunks_is_a_single_enter() -> None:
    kp = KeyParser()

    assert [k.control for k in kp.feed(b"\r")] == ["enter"]
    assert kp.feed(b"\n") == []


def test_two_enters_stay_two_enters() -> None:
    assert [k.control for k in KeyParser().feed(b"\r\r")] == ["enter", "enter"]


def test_lone_newline_is_still_enter() -> None:
    assert [k.control for k in KeyParser().feed(b"\n")] == ["enter"]


def test_text_followed_by_crlf() -> None:
    keys = KeyParser().feed(b"generate\r\n")

    assert "".join(k.char for k in keys) == "generate"
    assert [k.control for k in keys if k.control] == ["enter"]


def test_only_the_newline_after_cr_is_swallowed() -> None:
    assert [k.control for k in KeyParser().feed(b"\n\n")] == ["enter", "enter"]


# ---- 可见字符与控制字符 ----

def test_visible_chars() -> None:
    kp = KeyParser()
    keys = kp.feed(b"ab")
    assert [k.char for k in keys] == ["a", "b"]


def test_enter_and_backspace() -> None:
    kp = KeyParser()
    keys = kp.feed(b"a\r")
    assert keys[0].char == "a"
    assert keys[1].control == "enter"
    keys = kp.feed(b"\x7f")
    assert keys[0].control == "backspace"


def test_ctrl_keys() -> None:
    kp = KeyParser()
    keys = kp.feed(b"\x05\x07\x03")
    assert [k.control for k in keys] == ["ctrl_e", "ctrl_g", "ctrl_c"]


def test_utf8_split_across_feed() -> None:
    kp = KeyParser()
    b = "中".encode()
    assert kp.feed(b[:1]) == []
    assert kp.feed(b[1:])[0].char == "中"


def test_no_key_dropped() -> None:
    """一次 feed 多个键必须全部产出，不丢键。"""
    kp = KeyParser()
    keys = kp.feed(b"abc\r")
    assert len(keys) == 4
    assert keys[3].control == "enter"


# ---- 方向键（CSI 与 SS3 两种编码）----

@pytest.mark.parametrize(
    ("raw", "want"),
    [
        (b"\x1b[A", "up"),
        (b"\x1b[B", "down"),
        (b"\x1b[C", "right"),
        (b"\x1b[D", "left"),
        (b"\x1bOA", "up"),
        (b"\x1bOB", "down"),
        (b"\x1bOC", "right"),
        (b"\x1bOD", "left"),
        (b"\x1b[H", "home"),
        (b"\x1b[1~", "home"),
        (b"\x1b[F", "end"),
        (b"\x1b[4~", "end"),
        (b"\x1b[3~", "delete"),
    ],
)
def test_known_keys(raw: bytes, want: str) -> None:
    kp = KeyParser()
    keys = kp.feed(raw)
    assert len(keys) == 1
    assert keys[0].control == want


# ---- 终端应答：整段识别，零字符漏出 ----

@pytest.mark.parametrize(
    "raw",
    [
        b"\x1b[12;5R",                  # DSR 光标位置
        b"\x1b[?12;5R",                 # DSR 带问号
        b"\x1b[?1;2c",                  # DA1
        b"\x1b[?6c",                    # DA1 VT102
        b"\x1b[?62;22c",                # DA1 多参数
        b"\x1b[>41;354;0c",             # DA2
        b"\x1b[>0;10;1c",               # DA2 另一种
        b"\x1b[?0u",                    # kitty 键盘协议
        b"\x1b[?1u",
        b"\x1b[?12;1$y",                # DECRQM 模式报告（曾漏出 y）
        b"\x1b[8;24;80t",               # 窗口大小
        b"\x1b[1 q",                    # 带中间字节
        b"\x1bP>|XTerm(370)\x1b\\",     # DCS / XTVERSION
        b"\x1b]0;my title\x07",         # OSC 标题（BEL 收尾）
        b"\x1b]11;rgb:0000/0000/0000\x1b\\",  # OSC 背景色（ST 收尾）
    ],
)
def test_terminal_responses_never_leak(raw: bytes) -> None:
    """任何终端应答都必须整段识别，绝不产生可见字符。"""
    kp = KeyParser()
    keys = kp.feed(raw)
    assert len(keys) == 1, f"{raw!r} 产生了 {keys}"
    assert keys[0].control == "terminal_response", f"{raw!r} -> {keys[0]}"
    assert keys[0].raw == raw
    assert kp.pending_escape == b""


def test_decrqm_specifically_does_not_leak_y() -> None:
    """精确回归：``ESC[?12;1$y`` 曾在 ``$`` 处失败，残余 ``y`` 进了编辑器。"""
    kp = KeyParser()
    keys = kp.feed(b"\x1b[?12;1$y")
    assert [k.control for k in keys] == ["terminal_response"]
    assert all(k.char == "" for k in keys)


# ---- ESC 歧义 ----

def test_lone_escape_is_pending_then_flushed() -> None:
    kp = KeyParser()
    assert kp.feed(b"\x1b") == []
    assert kp.pending_escape == b"\x1b"
    key = kp.flush_escape()
    assert key is not None
    assert key.control == "escape"
    assert kp.pending_escape == b""


def test_escape_sequence_not_flushed_as_escape() -> None:
    """完整的方向键序列不应被误判成 Esc 键。"""
    kp = KeyParser()
    assert kp.feed(b"\x1b") == []
    keys = kp.feed(b"[A")
    assert [k.control for k in keys] == ["up"]
    assert kp.pending_escape == b""
    assert kp.flush_escape() is None


@pytest.mark.parametrize("raw", [b"\x1bx", b"\x1bq", b"\x1b7", b"\x1bc", b"\x1b="])
def test_escape_then_char_keeps_both(raw: bytes) -> None:
    """ESC 后紧跟可打印字符：判为「Esc 键 + 该字符」，两者都不丢。

    这些字节序列也可能是控制序列（如 ``ESC 7`` = DECSC），
    但它们只出现在程序**输出**中；输入流里的 ``ESC 7`` 只能是
    用户按了 Esc 再打 7，因此按「Esc + 字符」处理（readline 的语义）。
    """
    kp = KeyParser()
    keys = kp.feed(raw)
    assert keys[0].control == "escape"
    assert keys[1].char == chr(raw[1])
    assert kp.pending_escape == b""


# ---- 跨块切分一致性 ----

@pytest.mark.parametrize(
    "raw",
    [
        b"\x1b[12;5R",
        b"\x1b[?12;1$y",
        b"\x1b]0;t\x07",
        b"\x1b]11;rgb:1/1/1\x1b\\",
        b"\x1b[A",
        b"\x1bOA",
        b"\x1b[3~",
        b"\x1b[1 q",
    ],
)
def test_any_split_point_gives_same_result(raw: bytes) -> None:
    """字节在任意位置被切断，解析结果都必须一致。"""
    results = set()
    for cut in range(1, len(raw)):
        kp = KeyParser()
        keys = kp.feed(raw[:cut]) + kp.feed(raw[cut:])
        results.add((tuple(k.control for k in keys), "".join(k.char for k in keys)))
    assert len(results) == 1, f"{raw!r} 切分后结果不一致: {results}"
