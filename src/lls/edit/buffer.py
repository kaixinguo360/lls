"""行编辑器（移植自 legacy display.py 的 read_line 细腻逻辑）。

legacy 的精髓是用 VT 模拟的 Screen 当编辑缓冲，从而天然获得方向键、退格、
删除、Home/End、历史翻页等能力。这里把这些能力用独立的纯文本缓冲重新实现，
不再依赖 legacy Screen 的耦合，但保留全部编辑语义：

* 光标左右移动、Home/End；
* 退格（删光标前）、删除（删光标处）、插入/覆盖模式；
* 历史上下翻（readline 语义：翻页前保存草稿，翻回末尾恢复草稿）；
* 历史去重（连续相同不重复保存）；
* 宽字符正确（光标按显示列宽推进）。
"""

from __future__ import annotations

__all__ = ["LineEditor", "display_width", "render_prompt_line"]

# 字符显示宽度表（来自 legacy display.py，经实战验证）。
_CHAR_WIDTHS = [
    (126, 1), (159, 0), (687, 1), (710, 0), (711, 1),
    (727, 0), (733, 1), (879, 0), (1154, 1), (1161, 0),
    (4347, 1), (4447, 2), (7467, 1), (7521, 0), (8369, 1),
    (8426, 0), (9000, 1), (9002, 2), (11021, 1), (12350, 2),
    (12351, 1), (12438, 2), (12442, 0), (19893, 2), (19967, 1),
    (55203, 2), (63743, 1), (64106, 2), (65039, 1), (65059, 0),
    (65131, 2), (65279, 1), (65376, 2), (65500, 1), (65510, 2),
    (120831, 1), (262141, 2), (1114109, 1),
]


def _char_width(c: str) -> int:
    o = ord(c)
    if o == 0x0E or o == 0x0F:
        return 0
    for num, wid in _CHAR_WIDTHS:
        if o <= num:
            return wid
    return 1


def display_width(text: str) -> int:
    """字符串的显示宽度（按终端列）。"""
    return sum(_char_width(c) for c in text)


class LineEditor:
    """单行编辑器，带历史。"""

    def __init__(self, history: list[str] | None = None) -> None:
        self._history: list[str] = list(history or [])
        self._text = ""
        self._cursor = 0
        self._insert = True
        self._hist_idx: int | None = None  # None = 正在编辑新行
        self._draft = ""                   # 浏览历史前保存的草稿

    # ---- 只读 ----

    @property
    def text(self) -> str:
        return self._text

    @property
    def cursor(self) -> int:
        """光标位置（字符索引，非字节）。"""
        return self._cursor

    @property
    def history(self) -> list[str]:
        return self._history

    def reset(self, value: str = "") -> None:
        self._text = value
        self._cursor = len(value)
        self._hist_idx = None
        self._draft = ""

    # ---- 编辑 ----

    def insert(self, ch: str) -> None:
        if self._insert:
            self._text = self._text[: self._cursor] + ch + self._text[self._cursor :]
        else:
            # 覆盖模式：替换光标处字符
            self._text = (
                self._text[: self._cursor] + ch + self._text[self._cursor + 1 :]
            )
        self._cursor += 1

    def backspace(self) -> None:
        if self._cursor > 0:
            self._text = self._text[: self._cursor - 1] + self._text[self._cursor :]
            self._cursor -= 1

    def delete(self) -> None:
        if self._cursor < len(self._text):
            self._text = self._text[: self._cursor] + self._text[self._cursor + 1 :]

    # ---- 光标移动 ----

    def left(self) -> None:
        if self._cursor > 0:
            self._cursor -= 1

    def right(self) -> None:
        if self._cursor < len(self._text):
            self._cursor += 1

    def home(self) -> None:
        self._cursor = 0

    def end(self) -> None:
        self._cursor = len(self._text)

    # ---- 历史 ----

    def history_up(self) -> None:
        if not self._history:
            return
        if self._hist_idx is None:
            self._draft = self._text
            self._hist_idx = len(self._history)
        if self._hist_idx > 0:
            self._hist_idx -= 1
            self._text = self._history[self._hist_idx]
            self._cursor = len(self._text)

    def history_down(self) -> None:
        if self._hist_idx is None:
            return
        if self._hist_idx < len(self._history) - 1:
            self._hist_idx += 1
            self._text = self._history[self._hist_idx]
            self._cursor = len(self._text)
        else:
            # 翻回末尾：恢复草稿
            self._hist_idx = None
            self._text = self._draft
            self._cursor = len(self._text)

    # ---- 提交 ----

    def submit(self, *, keep: bool = True) -> str:
        """提交当前行：返回内容，默认加入历史（连续相同去重）。

        ``keep`` 为 False 时不进历史 —— 供「只是选一个键」的提示使用，
        这类输入事后没有召回价值，留在历史里只会污染 ↑。
        """
        text = self._text
        if keep and text and (not self._history or self._history[-1] != text):
            self._history.append(text)
        return text

    def record(self, value: str) -> None:
        """把一条内容记入历史（不改变当前编辑状态），连续相同去重。"""
        if value and (not self._history or self._history[-1] != value):
            self._history.append(value)


def render_prompt_line(prompt: str, editor: LineEditor, columns: int) -> tuple[str, int]:
    """生成带光标反色高亮的显示行，返回 (显示字符串, 光标列号)。

    光标位置按显示列宽计算（正确处理宽字符），高亮用 SGR 7（反色）。
    光标在行尾时高亮一个空格，表示"下一个字符位置"。
    """
    line = editor.text
    cursor = editor.cursor

    # 光标前的显示列数 = prompt 宽度 + line[:cursor] 宽度
    col = display_width(prompt) + display_width(line[:cursor])

    # 组装显示文本（截断到列宽，简单处理）
    full = prompt + line
    # 光标字符
    if cursor < len(line):
        ch = line[cursor]
        ch_width = max(_char_width(ch), 1)
    else:
        ch = " "
        ch_width = 1

    # 在光标位置插入反色高亮
    # 用字符串拼接：prompt + line 在光标处拆开
    before = prompt + line[:cursor]
    after = line[cursor + 1 :]
    display = before + "\x1b[7m" + ch + "\x1b[0m" + after

    # 截断到列宽（粗略：去掉超长部分，避免溢出）
    if display_width(full) + 0 > columns:
        # 简单截断：这里依赖 renderer 最终按列处理，先不做复杂截断
        pass

    return display, col
