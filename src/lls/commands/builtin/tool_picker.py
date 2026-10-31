"""工具开关界面：一列工具，空格切换，回车保存，q 取消。

版式与 :mod:`agent_picker` 同源：只画需要的行，浮在子进程画面末尾，退出时
把屏幕原样交还。区别只有一条 —— 候选不是选的，是**拨的**：空格就地翻转
``[on ]`` / ``[off]``，回车把当前开关状态交回去，``q`` / Esc 什么都不改。

这里不认识策略、会话或配置文件：只收一份 ``{名字: 是否启用}``，返回改过的
那一份。写回哪一层由调用方决定。
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Sequence

from lls.ui import clusters, conservative_char_width, text_width

__all__ = ["DEFAULT_FOOTER", "toggle_tools"]

_REVERSE = "\x1b[7m"
_RESET = "\x1b[0m"
_MARK = "> "
_BLANK = "  "
POLL_SECONDS = 0.2

DEFAULT_FOOTER = "up/down: move, space: on/off, Enter: save, q: cancel"

_ON = "[on ]"
_OFF = "[off]"
_ELLIPSIS = "..."
_ELLIPSIS_WIDTH = text_width(_ELLIPSIS, conservative_char_width)

_BY_CONTROL = {
    "up": "up",
    "down": "down",
    "pageup": "page_up",
    "pagedown": "page_down",
    "home": "first",
    "end": "last",
    "enter": "accept",
    "space": "toggle",
}

_BY_CHAR = {
    "j": "down",
    "k": "up",
    "g": "first",
    "G": "last",
    " ": "toggle",
}

_BY_RAW = {
    b"\x1b[5~": "page_up",
    b"\x1b[6~": "page_down",
    b"\x1b[1~": "first",
    b"\x1b[4~": "last",
}


def _clip(line: str, columns: int) -> str:
    if columns <= 0:
        return ""
    if text_width(line, conservative_char_width) <= columns:
        return line
    room = columns - _ELLIPSIS_WIDTH
    kept = ""
    used = 0
    for cluster in clusters(line):
        width = conservative_char_width(cluster)
        if used + width > room:
            break
        kept += cluster
        used += width
    return kept + _ELLIPSIS


def _intent(key: Any) -> str:
    """按键 → 意图；空串表示不处理。"""
    control = getattr(key, "control", "") or ""
    char = getattr(key, "char", "") or ""
    raw = getattr(key, "raw", b"") or b""
    if control in ("escape", "ctrl_c") or char in ("q", "Q"):
        return "quit"
    if raw in _BY_RAW:
        return _BY_RAW[raw]
    if control in _BY_CONTROL:
        return _BY_CONTROL[control]
    return _BY_CHAR.get(char, "")


def _size(services: Any) -> tuple:
    screen = services.screen
    return (
        int(getattr(screen, "rows", 24) or 24),
        int(getattr(screen, "columns", 80) or 80),
    )


async def toggle_tools(
    services: Any,
    names: Sequence[str],
    states: Dict[str, bool],
    *,
    header: str = "",
    footer: str = DEFAULT_FOOTER,
) -> Dict[str, bool] | None:
    """画出开关列表；回车返回改过的 ``{名字: bool}``，取消返回 ``None``。"""
    if not names:
        return None
    names = list(names)
    current = dict(states)
    rows, columns = _size(services)
    cursor = 0

    def frame() -> str:
        lines: List[str] = []
        if header:
            lines.append(_clip(" ".join(header.split()), columns))
        for index, name in enumerate(names):
            label = "{} {}".format(_ON if current[name] else _OFF, name)
            row = (_MARK if index == cursor else _BLANK) + label
            row = _clip(row, columns)
            if index == cursor:
                row = _REVERSE + row + _RESET
            lines.append(row)
        counted = footer
        if names:
            counted = "{}  {}/{}".format(footer, cursor + 1, len(names))
        lines.append(_clip(" ".join(counted.split()), columns))
        return "\n".join(lines)

    def render(rows_: int, columns_: int) -> str:
        nonlocal rows, columns
        rows, columns = max(1, int(rows_)), max(1, int(columns_))
        return frame()

    def move(delta: int) -> bool:
        nonlocal cursor
        new = min(len(names) - 1, max(0, cursor + delta))
        if new == cursor:
            return False
        cursor = new
        return True

    def apply(intent: str) -> bool:
        if intent == "down":
            return move(1)
        if intent == "up":
            return move(-1)
        if intent == "page_down":
            return move(max(1, rows - 3))
        if intent == "page_up":
            return move(-max(1, rows - 3))
        if intent == "first":
            return move(-len(names) - 1)
        if intent == "last":
            return move(len(names) + 1)
        if intent == "toggle":
            name = names[cursor]
            current[name] = not current[name]
            return True
        return False

    services.set_overlay_source(render)
    services.draw_overlay(frame())
    reader = asyncio.ensure_future(services.read_key())
    try:
        while True:
            done, _ = await asyncio.wait({reader}, timeout=POLL_SECONDS)
            if done:
                key = reader.result()
                if key is None:  # 子进程没了：当作取消
                    return None
                intent = _intent(key)
                if intent == "accept":
                    return dict(current)
                if intent == "quit":
                    return None
                apply(intent)
                reader = asyncio.ensure_future(services.read_key())
            services.draw_overlay(frame())
    finally:
        reader.cancel()
        services.set_overlay_source(None)
