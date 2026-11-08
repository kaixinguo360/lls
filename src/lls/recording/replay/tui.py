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
import unicodedata
from typing import Iterator

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
from .screen import Cursor

__all__ = ["run"]

# ---- 状态行的列宽：宁可少放字，也不能多占一格 ------------------------------
#
# 状态行画在最后一行上。多出一格，终端就在行尾自动折行 —— 最后一行折行等于把
# 整屏往上顶一行，播放时画面会一直往上爬。所以宽度按**排版口径**算：模糊宽度
# （``←`` ``→`` ``·`` ``…``）在不少中日文终端里是两列，就按两列算；emoji 变体、
# ZWJ 序列、成对的区域指示符按一个整簇算，裁切也不许把簇切成两半。
#
# 口径与 ``lls/ui/width.py`` 同源（直接问 ``unicodedata``，不抄第三方包的私有
# 表）。录制包不 import ``lls.*``，所以这里留一份等价实现；
# ``tests/unit/test_replay.py`` 有用 ``lls.ui.width`` 对齐的用例。

_ZWJ = "\u200d"
_VS16 = "\ufe0f"
_RI_FIRST = "\U0001f1e6"
_RI_LAST = "\U0001f1ff"
_MODIFIER_FIRST = "\U0001f3fb"
_MODIFIER_LAST = "\U0001f3ff"

# 零列的类别：组合符、包围符、格式符（含 VS16、ZWJ）、控制符。
_ZERO_CATEGORIES = ("Mn", "Me", "Cf", "Cc")

# 占两列的 EAW 取值。W/F 所有终端一致；A（模糊宽度）按两列是排版口径 ——
# 少放一个字最多显得保守，多放一个字就会折行、把整屏顶走。
_TWO_COLUMN_EAW = ("W", "F", "A")


def _codepoint_cells(char: str) -> int:
    if _MODIFIER_FIRST <= char <= _MODIFIER_LAST:  # 肤色修饰并进基字符
        return 0
    if _RI_FIRST <= char <= _RI_LAST:  # 区域指示符：成对是国旗，落单也画两列
        return 2
    if unicodedata.category(char) in _ZERO_CATEGORIES:
        return 0
    return 2 if unicodedata.east_asian_width(char) in _TWO_COLUMN_EAW else 1


def _cluster_cells(cluster: str) -> int:
    """整簇占几列：以第一个占列的码点为准，带 VS16 的一律按两列。"""
    cells = 0
    for char in cluster:
        char_cells = _codepoint_cells(char)
        if char_cells and not cells:
            cells = char_cells
    if _VS16 in cluster:
        cells = max(cells, 2)
    return cells


def _is_control(char: str) -> bool:
    return unicodedata.category(char) == "Cc"


def _is_regional(char: str) -> bool:
    return _RI_FIRST <= char <= _RI_LAST


def _cluster_end(text: str, index: int) -> int:
    end = index + 1
    regional = 1 if _is_regional(text[index]) else 0
    while end < len(text):
        char = text[end]
        if text[end - 1] == _ZWJ:
            joins = True
        elif _is_regional(char):
            joins = regional % 2 == 1  # 两个一对，第三个另起一簇
        else:
            joins = not _is_control(char) and _codepoint_cells(char) == 0
        if not joins:
            break
        if _is_regional(char):
            regional += 1
        end += 1
    return end


def _clusters(text: str) -> Iterator[str]:
    index = 0
    while index < len(text):
        end = _cluster_end(text, index)
        yield text[index:end]
        index = end


def _display_width(text: str) -> int:
    return sum(_cluster_cells(cluster) for cluster in _clusters(text))


def _fit_line(text: str, width: int) -> str:
    """按显示宽度裁到 ``width`` 列并补齐；整簇裁切，宽字符不会被劈成半格。"""
    if width <= 0:
        return ""
    used = 0
    kept: list[str] = []
    for cluster in _clusters(text):
        if _is_control(cluster[0]):
            continue  # 控制字符不写进状态行，避免换行/折返
        cells = _cluster_cells(cluster)
        if used + cells > width:
            break
        kept.append(cluster)
        used += cells
    return "".join(kept) + " " * (width - used)


def _tail(text: str, width: int) -> str:
    """取末尾不超过 ``width`` 列的整簇：宽字符与它后面的修饰不会被切成半个。"""
    kept: list[str] = []
    used = 0
    for cluster in reversed(list(_clusters(text))):
        cells = _cluster_cells(cluster)
        if used + cells > width:
            break
        kept.append(cluster)
        used += cells
    return "".join(reversed(kept))


def _cursor_sequence(cursor: Cursor, rows: int, columns: int) -> str:
    """把模型光标放回内容区：最后一行是状态行，光标不能停在它上面。"""
    visible = not cursor.hidden and 0 <= cursor.x < columns and 0 <= cursor.y < rows - 1
    if not visible:
        return "\x1b[?25l"
    return f"\x1b[?25h\x1b[{cursor.y + 1};{cursor.x + 1}H"


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
_KEY_SEQUENCES = tuple(sorted(_KEYS, key=len, reverse=True))

# CSI / SS3 的终止字节：方向键与 Home/End 不管带不带修饰键（``\x1b[1;2A``、
# ``\x1b[1;5H``）都按同一个键处理。
_CSI_KEYS = {
    b"A": "up",
    b"B": "down",
    b"C": "right",
    b"D": "left",
    b"H": "home",
    b"F": "end",
}

#: 只有单独的 Esc 才退出；序列没等到后续字节时最多等这么久。
_ESCAPE_GRACE = 0.05

#: 没有新帧时，状态行（进度、倍速）仍然按这个间隔重画，免得看起来像卡死。
_STATUS_REFRESH = 0.1


def _csi_end(buffer: bytes) -> int | None:
    """返回 CSI 序列的结束位置（不含）；参数还没到齐时返回 ``None``。"""
    index = 2  # 跳过 ESC [
    while index < len(buffer):
        byte = buffer[index]
        if 0x20 <= byte <= 0x3F:  # 参数字节与中间字节
            index += 1
            continue
        if 0x40 <= byte <= 0x7E:  # 终止字节
            return index + 1
        return index  # 序列里混进了非法字节：到这里为止
    return None


def _decode_key(buffer: bytes) -> tuple[str | None, bytes]:
    """解出一个按键；``None`` 表示前缀不完整，还要再等几个字节。

    方向键也是 Esc 开头：读端一次 ``read(64)`` 可能在序列中间切开（连续按方向
    键时 64 不是 3 的倍数，迟早切在 ESC 后面）。这时不能把半截序列当成 Esc，
    否则播放器会在按方向键时直接退出。
    """
    for sequence in _KEY_SEQUENCES:
        if buffer.startswith(sequence):
            return _KEYS[sequence], buffer[len(sequence) :]
    if buffer.startswith(b"\x1b["):
        end = _csi_end(buffer)
        if end is None:
            return None, buffer
        key = _CSI_KEYS.get(buffer[end - 1 : end])
        if key is not None:
            return key, buffer[end:]
        # 读不懂的 CSI（焦点上报、鼠标、修饰键组合……）整段吞掉，按「忽略」处理。
        return "ignore", buffer[end:]
    if buffer.startswith(b"\x1bO"):
        if len(buffer) < 3:
            return None, buffer
        key = _CSI_KEYS.get(buffer[2:3])
        return (key if key is not None else "ignore"), buffer[3:]
    if buffer[:1] == b"\x1b":
        if len(buffer) == 1:
            return None, buffer  # 可能是单独按 Esc，也可能是序列被 read 切断
        if buffer[1:2] == b"\x1b":
            return "escape", buffer[1:]
        # Alt+键 或读不懂的 ESC 前缀：连同后面那个字节一起吞掉，别当成退出。
        return "ignore", buffer[2:]
    if buffer[:1] == b"\x03":
        return "ctrl-c", buffer[1:]
    if buffer[:1] in (b"\r", b"\n"):
        return "enter", buffer[1:]
    if buffer[:1] == b"\x7f":
        return "backspace", buffer[1:]
    if buffer and 32 <= buffer[0] < 127:
        return chr(buffer[0]), buffer[1:]
    if buffer:
        return "ignore", buffer[1:]
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
        start_frame: int | None,
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
        self._paused = False
        self._keybuf = b""
        self._last_input = b""
        self._last_size = (0, 0)
        self._last_paint = 0.0
        # 方向键只登记目标，等输入流停下来再重建一次；连按不会每次从头 seek。
        self._pending_seek: float | None = None
        self._locating: float | None = None
        if start_frame is not None:
            self._playback.seek_frame(start_frame)
            start = self._playback.position
            self._frames = recording.frames(start=start_frame + 1)
            self._next = next(self._frames, None)
        else:
            start = min(max(start_at, 0.0), self._duration)
            self._playback.seek(start)
            self._frames = recording.frames()
            self._next = next(self._frames, None)
            while self._next is not None and self._next.at <= start + TIME_EPSILON:
                if self._next.channel == "i":
                    self._last_input = self._next.data
                self._next = next(self._frames, None)
        self._paused_at = start
        self._origin_at = start
        self._origin_wall = time.monotonic()
        self._ended = self._next is None

    # ---- 主循环 ----

    def run(self) -> int:
        self._paint(full=True)
        while True:
            size = self._terminal_size()
            if size != self._last_size:
                self._last_size = size
                self._paint(full=True)
            self._flush_seek()
            key = self._read_key(self._timeout())
            if key is None:
                if not self._advance():
                    self._refresh_status()
                continue
            if not self._handle(key):
                return 0

    def _terminal_size(self) -> tuple[int, int]:
        size = shutil.get_terminal_size((80, 24))
        if size.lines <= 0 or size.columns <= 0:
            return 80, 24
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

    def _advance(self) -> bool:
        """推进到当前时刻；返回是否已经重画过。"""
        if self._paused:
            return False
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
            changed = True
        return changed

    def _refresh_status(self) -> None:
        """没有新帧时也重画状态行：播放进度不会看起来停在原地。"""
        if self._paused or self._ended:
            return
        if time.monotonic() - self._last_paint >= _STATUS_REFRESH:
            self._paint()

    # ---- 按键 ----

    def _read_key(self, timeout: float) -> str | None:
        if not self._keybuf:
            ready, _, _ = select.select([self._terminal.fd], [], [], timeout)
            if not ready:
                return None
            data = self._read_bytes()
            if not data:
                return "escape"
            self._keybuf += data
        # 单独的 ESC 与「序列被 read 切开」只能靠时间区分：先等一小会儿，
        # 等到后续字节就是方向键，等不到才是真的按了 Esc。
        deadline = time.monotonic() + _ESCAPE_GRACE
        while True:
            key, rest = _decode_key(self._keybuf)
            if key is not None:
                self._keybuf = rest
                return key
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                pending, self._keybuf = self._keybuf, b""
                return "escape" if pending == b"\x1b" else "ignore"
            ready, _, _ = select.select(
                [self._terminal.fd], [], [], min(remaining, 0.05)
            )
            if not ready:
                continue
            data = self._read_bytes()
            if not data:
                pending, self._keybuf = self._keybuf, b""
                return "escape" if pending == b"\x1b" else "ignore"
            self._keybuf += data

    def _read_bytes(self) -> bytes:
        try:
            return os.read(self._terminal.fd, 64)
        except OSError:
            return b""

    def _input_waiting(self) -> bool:
        if self._keybuf:
            return True
        ready, _, _ = select.select([self._terminal.fd], [], [], 0)
        return bool(ready)

    def _handle(self, key: str) -> bool:
        if key in ("q", "escape", "ctrl-c"):
            return False
        if key == "ignore":
            return True
        if key == " ":
            self._apply_pending_seek()
            self._toggle_pause()
        elif key == "left":
            self._seek_relative(-5)
        elif key == "right":
            self._seek_relative(5)
        elif key == "up":
            self._seek_relative(-1)
        elif key == "down":
            self._seek_relative(1)
        elif key in ("home", "g"):
            self._request_seek(0)
        elif key in ("end", "G"):
            self._request_seek(self._duration)
        elif key == "page-up":
            self._seek_relative(-30)
        elif key == "page-down":
            self._seek_relative(30)
        elif key == "[":
            self._apply_pending_seek()
            self._change_speed(1 / 1.5)
        elif key == "]":
            self._apply_pending_seek()
            self._change_speed(1.5)
        elif key == "i":
            self._apply_pending_seek()
            self._show_input = not self._show_input
            self._paint()
        elif key in (",", "."):
            self._seek_relative(0.1 if key == "." else -0.1)
        elif key == "s":
            self._apply_pending_seek()
            self._paint()
        return True

    # ---- 定位 ----

    def _seek_relative(self, delta: float) -> None:
        base = self._pending_seek
        if base is None:
            base = self._position_now()
        self._request_seek(base + delta)

    def _request_seek(self, target: float) -> None:
        self._pending_seek = min(max(target, 0.0), self._duration)

    def _flush_seek(self) -> None:
        if self._pending_seek is not None and not self._input_waiting():
            self._apply_pending_seek()

    def _apply_pending_seek(self) -> None:
        if self._pending_seek is None:
            return
        target, self._pending_seek = self._pending_seek, None
        self._seek(target)

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
        if target < self._playback.position - TIME_EPSILON:
            # 终端状态是累积的，往回只能从头重建；这段时间给个「定位中」。
            self._rebuild(target)
        else:
            self._forward(target)
        self._paused_at = target
        self._origin_at = target
        self._origin_wall = time.monotonic()
        self._ended = self._next is None
        self._paint(full=True)

    def _forward(self, target: float) -> None:
        """向前定位：接着当前的帧游标放，不重置屏幕模型。"""
        while self._next is not None and self._next.at <= target + TIME_EPSILON:
            frame = self._next
            if frame.channel == "i":
                self._last_input = frame.data
            self._playback.apply(frame)
            self._next = next(self._frames, None)
        self._playback.set_position(target)

    def _rebuild(self, target: float) -> None:
        self._locating = target
        self._draw_status()
        self._playback.reset()
        frames = self._recording.frames()
        self._next = None
        for frame in frames:
            if frame.at > target + TIME_EPSILON:
                self._next = frame
                break
            if frame.channel == "i":
                self._last_input = frame.data
            self._playback.apply(frame)
        self._frames = frames
        self._playback.set_position(target)
        self._locating = None

    # ---- 绘制 ----

    def _paint(self, *, full: bool = False) -> None:
        self._painter.paint(self._playback.screen, full=full)
        self._draw_status()
        # 状态行占着最后一行，而画它会把光标留在行尾；把模型光标放回去，
        # 否则播放时光标会在内容区和状态行之间来回跳。
        rows, columns = self._terminal_size()
        self._terminal.out.write(
            _cursor_sequence(self._playback.screen.cursor, rows, columns).encode()
        )
        self._terminal.out.flush()
        self._last_paint = time.monotonic()

    def _draw_status(self) -> None:
        rows, columns = self._terminal_size()
        width = columns - 1  # 留最后一列，避免行尾自动折返
        if rows <= 0 or width <= 0:
            return
        if self._locating is not None:
            state, position = "定位中", self._locating
        else:
            state = "暂停" if self._paused else "结束" if self._ended else "播放"
            position = self._position_now()
        text = (
            f"{self._recording.meta.id}  {position:.1f}/"
            f"{self._duration:.1f}s  {self._speed:.1f}x  {state}"
            f"{self._input_label()}  [空格]暂停 [←→]5s [q]退出"
        )
        self._terminal.out.write(
            (f"\x1b[{rows};1H\x1b[7m{_fit_line(text, width)}\x1b[0m").encode()
        )
        self._terminal.out.flush()

    def _input_label(self) -> str:
        if not self._show_input or not self._last_input:
            return ""
        text = self._last_input.decode("utf-8", "replace")
        visible = "".join(char if char.isprintable() else "·" for char in text)
        return "  输入:" + _tail(visible, 24)


def run(
    recording: Recording,
    *,
    size: tuple[int, int],
    start_at: float = 0.0,
    start_frame: int | None = None,
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
            start_frame=start_frame,
            speed=speed,
            show_input=show_input,
            screen_factory=screen_factory,
            row_renderer=row_renderer,
        )
        return player.run()
