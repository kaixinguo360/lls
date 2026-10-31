"""工具开关界面：一列工具，空格拨开关，``r`` 回到跟随全局，回车保存。

版式与 :mod:`agent_picker` 同源：只画需要的行，浮在子进程画面末尾，退出时
把屏幕原样交还。两个区别：

* 候选不是选的，是**拨的**：空格翻转 ``[on ]`` / ``[off]``；
* 行尾标出这一项是**谁说的**：``session`` 表示本会话显式设置过，
  ``global`` 表示跟随全局（``r`` 可以清掉本会话的设置，回到跟随）。

``None``（这一层没说）因此只体现在来源列上，不当作第三个开关值 —— 开关
始终二态，撤销是一个动作。

这里不认识策略、会话或配置文件：只收一份 ``ToolSwitch`` 列表，返回改过的
那一份。写回哪一层由调用方决定。

界面原语层保持 ASCII（与 picker / pager 一致），中文提示由调用方放进 header。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, List, Optional, Sequence

from lls.ui import clusters, conservative_char_width, text_width

__all__ = ["DEFAULT_FOOTER", "FOLLOW_FOOTER", "ToolSwitch", "toggle_tools"]

_REVERSE = "\x1b[7m"
_RESET = "\x1b[0m"
_MARK = "> "
_BLANK = "  "
POLL_SECONDS = 0.2

DEFAULT_FOOTER = "up/down: move, space: on/off, r: follow global, Enter: save, q: cancel"
FOLLOW_FOOTER = "up/down: move, space: on/off, Enter: save, q: cancel"

#: 来源列的取值。
SOURCE_SESSION = "session"
SOURCE_GLOBAL = "global"

_ON = "[on ]"
_OFF = "[off]"
_NAME_WIDTH = 15  # read_terminal 是 13，留两格
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
    "r": "follow",
}

_BY_RAW = {
    b"\x1b[5~": "page_up",
    b"\x1b[6~": "page_down",
    b"\x1b[1~": "first",
    b"\x1b[4~": "last",
}


@dataclass(frozen=True)
class ToolSwitch:
    """界面里的一行。

    ``enabled`` 是**生效值**（两层合并之后）；``source`` 说这个值从哪里来：
    本会话显式设置（``session``）还是跟随全局（``global``）。``fallback``
    是全局值，``r`` 恢复跟随的时候用它。
    """

    name: str
    enabled: bool
    source: str = SOURCE_GLOBAL
    fallback: bool = True


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
    switches: Sequence[ToolSwitch],
    *,
    header: str = "",
    footer: str = "",
    allow_follow: bool = True,
) -> Optional[List[ToolSwitch]]:
    """画出开关列表；回车返回改过的一份，取消返回 ``None``。

    ``allow_follow`` 为 False 时没有 ``r``（全局层没有可以跟随的上一层）。
    """
    if not switches:
        return None
    items = list(switches)
    rows, columns = _size(services)
    cursor = 0
    text_footer = footer or (DEFAULT_FOOTER if allow_follow else FOLLOW_FOOTER)

    def frame() -> str:
        lines: List[str] = []
        if header:
            lines.append(_clip(" ".join(header.split()), columns))
        for index, item in enumerate(items):
            label = "{} {:<{width}}".format(
                _ON if item.enabled else _OFF, item.name, width=_NAME_WIDTH
            )
            row = (_MARK if index == cursor else _BLANK) + label + " " + item.source
            row = _clip(row, columns)
            if index == cursor:
                row = _REVERSE + row + _RESET
            lines.append(row)
        counted = "{}  {}/{}".format(text_footer, cursor + 1, len(items))
        lines.append(_clip(" ".join(counted.split()), columns))
        return "\n".join(lines)

    def render(rows_: int, columns_: int) -> str:
        nonlocal rows, columns
        rows, columns = max(1, int(rows_)), max(1, int(columns_))
        return frame()

    def move(delta: int) -> bool:
        nonlocal cursor
        new = min(len(items) - 1, max(0, cursor + delta))
        if new == cursor:
            return False
        cursor = new
        return True

    def apply(intent: str) -> None:
        nonlocal cursor
        if intent in ("down", "up", "page_down", "page_up", "first", "last"):
            if intent == "down":
                move(1)
            elif intent == "up":
                move(-1)
            elif intent == "page_down":
                move(max(1, rows - 3))
            elif intent == "page_up":
                move(-max(1, rows - 3))
            elif intent == "first":
                move(-len(items) - 1)
            else:
                move(len(items) + 1)
            return
        item = items[cursor]
        if intent == "toggle":
            items[cursor] = ToolSwitch(
                name=item.name,
                enabled=not item.enabled,
                source=SOURCE_SESSION,
                fallback=item.fallback,
            )
        elif intent == "follow" and allow_follow:
            items[cursor] = ToolSwitch(
                name=item.name,
                enabled=item.fallback,
                source=SOURCE_GLOBAL,
                fallback=item.fallback,
            )

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
                    return list(items)
                if intent == "quit":
                    return None
                apply(intent)
                reader = asyncio.ensure_future(services.read_key())
            services.draw_overlay(frame())
    finally:
        reader.cancel()
        services.set_overlay_source(None)
