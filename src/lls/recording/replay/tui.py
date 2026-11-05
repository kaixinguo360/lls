"""全屏回放界面：raw 模式、备用屏、按键与时钟。

时钟只在这里出现；``Playback`` 保持纯状态机，便于用 ``--dump`` 或测试直接
驱动。
"""

from __future__ import annotations

import contextlib
import os
import select
import shutil
import sys
import time

try:  # 只有 POSIX 有；没有时 play 会给出提示，export/dump 仍然可用。
    import termios
    import tty
except ImportError:  # pragma: no cover - 非 POSIX
    termios = None  # type: ignore[assignment]
    tty = None  # type: ignore[assignment]

from ..format import TIME_EPSILON
from ..reader import Recording
from .paint import RowRenderer, ScreenPainter
from .player import Playback, ScreenFactory

__all__ = ["run"]

_KEYS = {
    b"\x1b[A": "up",
    b"\x1b[B": "down",
    b"\x1b[C": "right",
    b"\x1b[D": "left",
    b"\x1b[H": "home",
    b"\x1b[F": "end",
    b"\x1b[1~": "home",
    b"\x1b[4~": "end",
    b"\x1b[5~": "page-up",
    b"\x1b[6~": "page-down",
}


def _decode_key(buffer: bytes) -> tuple[str | None, bytes]:
    for sequence in sorted(_KEYS, key=len, reverse=True):
        if buffer.startswith(sequence):
            return _KEYS[sequence], buffer[len(sequence) :]
    if buffer == b"\x1b":
        return "escape", b""
    if buffer.startswith(b"\x1b["):
        # 序列可能被 read 切断；再等一小会儿。
        return None, buffer
    if buffer[:1] == b"\x1b":
        return "escape", buffer[1:]
    if buffer[:1] == b"\x03":
        return "ctrl-c", buffer[1:]
    if buffer[:1] in (b"\r", b"\n"):
        return "enter", buffer[1:]
    if buffer[:1] == b"\x7f":
        return "backspace", buffer[1:]
    if buffer and 32 <= buffer[0] < 127:
        return chr(buffer[0]), buffer[1:]
    if buffer:
        return "escape", buffer[1:]
    return None, buffer


class _Terminal:
    def __init__(self) -> None:
        self.fd = sys.stdin.fileno()
        self.out = sys.stdout.buffer
        self.old: list | None = None

    def __enter__(self) -> _Terminal:
        self.old = termios.tcgetattr(self.fd)  # type: ignore[union-attr]
        tty.setraw(self.fd)  # type: ignore[union-attr]
        self.out.write(b"\x1b[?1049h\x1b[?25l\x1b[H\x1b[2J")
        self.out.flush()
        return self

    def __exit__(self, *exc: object) -> None:
        with contextlib.suppress(OSError):
            self.out.write(b"\x1b[0m\x1b[?25h\x1b[?1049l")
            self.out.flush()
        if self.old is not None:
            with contextlib.suppress(OSError):
                termios.tcsetattr(  # type: ignore[union-attr]
                    self.fd, termios.TCSADRAIN, self.old  # type: ignore[union-attr]
                )


class _Player:
    def __init__(
        self,
        terminal: _Terminal,
        recording: Recording,
        *,
        size: tuple[int, int],
        start_at: float,
        speed: float,
        show_input: bool,
        screen_factory: ScreenFactory | None,
        row_renderer: RowRenderer | None,
    ) -> None:
        self._terminal = terminal
        self._recording = recording
        self._duration = max(0.0, recording.duration()) + TIME_EPSILON
        self._speed = speed
        self._show_input = show_input
        self._playback = Playback(
            recording,
            size=size,
            screen_factory=screen_factory,
        )
        self._painter = ScreenPainter(
            terminal.out,
            row_renderer=row_renderer,
            viewport=self._viewport,
        )
        start = min(max(start_at, 0.0), self._duration)
        self._paused = False
        self._paused_at = start
        self._origin_at = start
        self._origin_wall = time.monotonic()
        self._ended = False
        self._keybuf = b""
        self._last_input = b""
        self._last_size = (0, 0)
        self._playback.seek(start)
        self._frames = recording.frames()
        self._next = next(self._frames, None)
        while self._next is not None and self._next.at <= start + TIME_EPSILON:
            if self._next.channel == "i":
                self._last_input = self._next.data
            self._next = next(self._frames, None)
        self._ended = self._next is None

    # ---- 主循环 ----

    def run(self) -> int:
        self._paint(full=True)
        while True:
            size = self._terminal_size()
            if size != self._last_size:
                self._last_size = size
                self._paint(full=True)
            key = self._read_key(self._timeout())
            if key is None:
                self._advance()
                continue
            if not self._handle(key):
                return 0

    def _terminal_size(self) -> tuple[int, int]:
        size = shutil.get_terminal_size((80, 24))
        return size.lines, size.columns

    def _viewport(self) -> tuple[int, int]:
        return self._terminal_size()

    # ---- 时间 ----

    def _position_now(self) -> float:
        if self._paused:
            return min(self._duration, self._paused_at)
        elapsed = (time.monotonic() - self._origin_wall) * self._speed
        return min(self._duration, self._origin_at + elapsed)

    def _timeout(self) -> float:
        if self._paused or self._next is None:
            return 0.1
        remaining = self._next.at - self._position_now()
        if remaining <= 0:
            return 0
        return min(remaining / self._speed, 0.1)

    def _advance(self) -> None:
        if self._paused:
            return
        position = self._position_now()
        changed = False
        while self._next is not None and self._next.at <= position:
            frame = self._next
            if frame.channel == "i":
                self._last_input = frame.data
                if self._show_input:
                    changed = True
            if self._playback.apply(frame):
                changed = True
            self._next = next(self._frames, None)
        if changed:
            self._paint()
        if self._next is None and not self._ended:
            self._ended = True
            self._paint()

    # ---- 按键 ----

    def _read_key(self, timeout: float) -> str | None:
        if not self._keybuf:
            ready, _, _ = select.select([self._terminal.fd], [], [], timeout)
            if not ready:
                return None
            try:
                data = os.read(self._terminal.fd, 64)
            except OSError:
                return "escape"
            if not data:
                return "escape"
            self._keybuf += data
        key, rest = _decode_key(self._keybuf)
        if key is None:
            # 序列不完整：再等一次；仍然不够就按 Esc 处理。
            ready, _, _ = select.select([self._terminal.fd], [], [], 0.05)
            if ready:
                with contextlib.suppress(OSError):
                    self._keybuf += os.read(self._terminal.fd, 64)
                key, rest = _decode_key(self._keybuf)
            if key is None:
                # 等不到后续字节：整段按 Esc 处理，别把半截序列当普通键。
                key, rest = "escape", b""
        self._keybuf = rest
        return key

    def _handle(self, key: str) -> bool:
        if key in ("q", "escape", "ctrl-c"):
            return False
        if key == " ":
            self._toggle_pause()
        elif key == "left":
            self._seek(self._position_now() - 5)
        elif key == "right":
            self._seek(self._position_now() + 5)
        elif key == "up":
            self._seek(self._position_now() - 1)
        elif key == "down":
            self._seek(self._position_now() + 1)
        elif key in ("home", "g"):
            self._seek(0)
        elif key in ("end", "G"):
            self._seek(self._duration)
        elif key == "page-up":
            self._seek(self._position_now() - 30)
        elif key == "page-down":
            self._seek(self._position_now() + 30)
        elif key == "[":
            self._change_speed(1 / 1.5)
        elif key == "]":
            self._change_speed(1.5)
        elif key == "i":
            self._show_input = not self._show_input
            self._paint()
        elif key in (",", "."):
            self._seek(self._position_now() + (0.1 if key == "." else -0.1))
        elif key == "s":
            self._paint()
        return True

    def _toggle_pause(self) -> None:
        if self._paused:
            self._paused = False
            self._origin_at = self._paused_at
            self._origin_wall = time.monotonic()
        else:
            self._paused_at = self._position_now()
            self._paused = True
        self._paint()

    def _change_speed(self, factor: float) -> None:
        position = self._position_now()
        self._speed = max(0.1, min(16.0, self._speed * factor))
        self._origin_at = position
        self._origin_wall = time.monotonic()
        self._paint()

    def _seek(self, target: float) -> None:
        target = min(max(target, 0.0), self._duration)
        self._playback.seek(target)
        self._frames = self._recording.frames()
        self._next = next(self._frames, None)
        while self._next is not None and self._next.at <= target + TIME_EPSILON:
            if self._next.channel == "i":
                self._last_input = self._next.data
            self._next = next(self._frames, None)
        self._paused_at = target
        self._origin_at = target
        self._origin_wall = time.monotonic()
        self._ended = self._next is None
        self._paint(full=True)

    # ---- 绘制 ----

    def _paint(self, *, full: bool = False) -> None:
        self._painter.paint(self._playback.screen, full=full)
        self._draw_status()

    def _draw_status(self) -> None:
        rows, columns = self._terminal_size()
        if rows <= 0 or columns <= 0:
            return
        if self._paused:
            state = "暂停"
        elif self._ended:
            state = "结束"
        else:
            state = "播放"
        text = (
            f"{self._recording.meta.id}  {self._position_now():.1f}/"
            f"{self._duration:.1f}s  {self._speed:.1f}x  {state}"
            f"{self._input_label()}  [空格]暂停 [←→]5s [q]退出"
        )
        text = text[: max(0, columns - 1)]
        self._terminal.out.write(
            (f"\x1b[{rows};1H\x1b[7m{text.ljust(columns - 1)}\x1b[0m").encode()
        )
        self._terminal.out.flush()

    def _input_label(self) -> str:
        if not self._show_input or not self._last_input:
            return ""
        text = self._last_input.decode("utf-8", "replace")
        visible = "".join(char if char.isprintable() else "·" for char in text)
        return "  输入:" + visible[-24:]


def run(
    recording: Recording,
    *,
    size: tuple[int, int],
    start_at: float = 0.0,
    speed: float = 1.0,
    show_input: bool = False,
    screen_factory: ScreenFactory | None = None,
    row_renderer: RowRenderer | None = None,
) -> int:
    """在 tty 上播放；调用方负责先确认 stdin/stdout 是终端。"""
    if termios is None or tty is None:
        print("lls recording: 当前平台不支持交互回放，请用 --dump 或 export", file=sys.stderr)
        return 1
    with _Terminal() as terminal:
        player = _Player(
            terminal,
            recording,
            size=size,
            start_at=start_at,
            speed=speed,
            show_input=show_input,
            screen_factory=screen_factory,
            row_renderer=row_renderer,
        )
        return player.run()
