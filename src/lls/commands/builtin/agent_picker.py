"""A small picker: one column of candidates, one cursor, Enter to choose.

Same spirit as :mod:`agent_pager`: layout and key handling only, it knows
nothing about sessions or models. The difference is that it does **not** fill
the screen: the overlay is only as tall as the candidate rows plus one footer
line, floats at the end of the child's screen, and is handed back as it was
when it closes. A windowed view kicks in only when the candidates outgrow the
screen, and that window follows the cursor.

Keys come from ``Services.read_key``: arrow keys may arrive split across reads,
so the engine's key parser has to assemble them.

Everything here is plain ASCII: no fancy glyphs, no CJK punctuation.
"""

from __future__ import annotations

import asyncio
from typing import Any, List, Sequence

from lls.ui import conservative_char_width, text_width

__all__ = ["Picker", "pick"]

_REVERSE = "\x1b[7m"
_RESET = "\x1b[0m"

# Poll the window size even when no key arrives: dragging the terminal edge
# should re-layout right away.
POLL_SECONDS = 0.2

ELLIPSIS = "..."
_ELLIPSIS_WIDTH = sum(conservative_char_width(c) for c in ELLIPSIS)

# Cursor marker: the selected row is reversed and gets a small pointer.
_MARK = "> "
_BLANK = "  "

DEFAULT_FOOTER = "up/down select - Enter switch - q cancel"
EMPTY_ROW = "(none)"

_BY_CONTROL = {
    "up": "up",
    "down": "down",
    "pageup": "page_up",
    "pagedown": "page_down",
    "home": "first",
    "end": "last",
    "enter": "accept",
}

_BY_CHAR = {
    "j": "down",
    "k": "up",
    "g": "first",
    "G": "last",
}

_BY_RAW = {
    b"\x1b[5~": "page_up",
    b"\x1b[6~": "page_down",
    b"\x1b[1~": "first",
    b"\x1b[4~": "last",
}


def _clip(line: str, columns: int) -> str:
    """Truncate to ``columns``; an overlong line ends with ``...``."""
    if columns <= 0:
        return ""
    if text_width(line, conservative_char_width) <= columns:
        return line
    room = columns - _ELLIPSIS_WIDTH
    kept = ""
    used = 0
    for char in line:
        width = conservative_char_width(char)
        if used + width > room:
            break
        kept += char
        used += width
    return kept + ELLIPSIS


def _intent(key: Any) -> str:
    """Turn a key into an intent; an empty string means no action."""
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


class Picker:
    """Candidates plus a cursor and a window that only as tall as needed."""

    def __init__(
        self,
        items: Sequence[Any],
        labels: Sequence[str],
        *,
        rows: int,
        columns: int,
        cursor: int = 0,
        footer: str = "",
        header: str = "",
    ) -> None:
        self._items = list(items)
        self._labels = list(labels)
        self._rows = rows
        self._columns = columns
        self._footer = footer
        self._header = header
        self._cursor = 0
        self._top = 0
        self._height = 1
        self._layout(cursor)

    def _layout(self, cursor: int | None = None) -> None:
        total = len(self._items)
        self._columns = max(1, self._columns)
        # The row count does not chase the screen height: take only the rows
        # that are actually needed.
        # Two lines go to chrome (footer, and a header when there is one).
        chrome = 2 + (1 if self._header else 0)
        self._height = max(1, min(total or 1, max(1, self._rows - chrome)))
        if cursor is not None:
            self._cursor = min(max(0, cursor), max(0, total - 1))
        if self._cursor < self._top:
            self._top = self._cursor
        elif self._cursor >= self._top + self._height:
            self._top = self._cursor - self._height + 1
        self._top = max(0, min(self._top, max(0, total - self._height)))

    @property
    def cursor(self) -> int:
        return self._cursor

    def resize(self, rows: int, columns: int) -> bool:
        """Re-layout when the window size changed; True if it really did."""
        if (rows, columns) == (self._rows, self._columns):
            return False
        self._rows, self._columns = rows, columns
        self._layout()
        return True

    def frame(self) -> str:
        """This screen: a header, the visible candidates, one footer line."""
        total = len(self._items)
        body = []
        if self._header:
            body.append(_clip(" ".join(self._header.split()), self._columns))
        for index in range(self._top, min(total, self._top + self._height)):
            label = _clip(self._labels[index], self._columns - 2)
            if index == self._cursor:
                body.append(_REVERSE + (_MARK + label)[: self._columns] + _RESET)
            else:
                body.append(_BLANK + label)
        if not body:
            body = [_BLANK + EMPTY_ROW]
        footer = self._footer
        if total:
            footer = "{}  {}/{}".format(footer, self._cursor + 1, total).strip()
        body.append(_clip(" ".join(footer.split()), self._columns))
        return "\n".join(body)

    def move(self, delta: int) -> bool:
        """Move the cursor; False when it already sits on an edge."""
        new = min(len(self._items) - 1, max(0, self._cursor + delta))
        if new == self._cursor:
            return False
        self._layout(new)
        return True

    def handle(self, key: Any) -> str:
        """Handle one key: ``accept`` / ``quit`` / ``redraw`` / ``none``."""
        intent = _intent(key)
        if intent in ("accept", "quit"):
            return intent
        return "redraw" if self._apply(intent) else "none"

    def _apply(self, intent: str) -> bool:
        if intent == "down":
            return self.move(1)
        if intent == "up":
            return self.move(-1)
        if intent == "page_down":
            return self.move(max(1, self._height))
        if intent == "page_up":
            return self.move(-max(1, self._height))
        if intent == "first":
            return self.move(-len(self._items) - 1)
        if intent == "last":
            return self.move(len(self._items) + 1)
        return False

    def selected(self) -> Any:
        if 0 <= self._cursor < len(self._items):
            return self._items[self._cursor]
        return None


def _size(services: Any) -> tuple:
    screen = services.screen
    return (
        int(getattr(screen, "rows", 24) or 24),
        int(getattr(screen, "columns", 80) or 80),
    )


async def pick(
    services: Any,
    items: Sequence[Any],
    labels: Sequence[str],
    *,
    cursor: int = 0,
    footer: str = DEFAULT_FOOTER,
    header: str = "",
) -> Any:
    """Draw the picker, return the chosen element; None if cancelled."""
    if not items:
        return None
    rows, columns = _size(services)
    picker = Picker(
        items,
        labels,
        rows=rows,
        columns=columns,
        cursor=cursor,
        footer=footer,
        header=header,
    )

    def render(rows: int, columns: int) -> str:
        picker.resize(rows, columns)
        return picker.frame()

    services.set_overlay_source(render)
    services.draw_overlay(picker.frame())
    reader = asyncio.ensure_future(services.read_key())
    try:
        while True:
            done, _ = await asyncio.wait({reader}, timeout=POLL_SECONDS)
            resized = picker.resize(*_size(services))
            moved = False
            if done:
                key = reader.result()
                if key is None:          # child process gone: treat as cancel
                    return None
                action = picker.handle(key)
                if action == "accept":
                    return picker.selected()
                if action == "quit":
                    return None
                moved = action == "redraw"
                reader = asyncio.ensure_future(services.read_key())
            if resized or moved:
                services.draw_overlay(picker.frame())
    finally:
        reader.cancel()
        services.set_overlay_source(None)
        services.restore_context()
