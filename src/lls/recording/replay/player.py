"""回放状态机：把帧序列喂给屏幕模型，不碰时钟、按键与终端。"""

from __future__ import annotations

from typing import Callable

from ..format import CHANNEL_OUTPUT, CHANNEL_RESIZE, TIME_EPSILON, parse_resize
from ..model import Frame
from ..reader import Recording
from .screen import ReplayScreen, make_screen

__all__ = ["Playback"]

ScreenFactory = Callable[[int, int], ReplayScreen]


class Playback:
    """一段录制的可 seeking 回放状态。"""

    def __init__(
        self,
        recording: Recording,
        *,
        size: tuple[int, int] | None = None,
        screen_factory: ScreenFactory | None = None,
    ) -> None:
        self.recording = recording
        self._factory = screen_factory or make_screen
        self._size = size or recording.geometry or (24, 80)
        self.screen = self._new_screen()
        self._position = 0.0

    @property
    def position(self) -> float:
        return self._position

    @property
    def duration(self) -> float:
        return self.recording.duration()

    def _new_screen(self) -> ReplayScreen:
        rows, columns = self._size
        return self._factory(rows, columns)

    def reset(self) -> None:
        self.screen = self._new_screen()
        self._position = 0.0

    def set_position(self, at: float) -> None:
        """把逻辑位置移到 ``at``；调用方保证画面已经喂到那里。"""
        self._position = max(0.0, at)

    def apply(self, frame: Frame) -> bool:
        """应用一帧；返回画面是否可能变化（``i`` 等通道返回 ``False``）。"""
        self._position = max(self._position, frame.at)
        if frame.channel == CHANNEL_OUTPUT:
            self.screen.feed(frame.data)
            return True
        if frame.channel == CHANNEL_RESIZE:
            size = parse_resize(frame.data)
            if size is not None:
                self.screen.resize(*size)
                return True
        return False

    def seek(self, at: float) -> None:
        """重置并快速重放到 ``at``；终端状态是累积的，只能从头重建。"""
        self.reset()
        target = max(0.0, at)
        for frame in self.recording.frames():
            if frame.at > target + TIME_EPSILON:
                break
            self.apply(frame)
        self._position = target

    def seek_frame(self, number: int) -> None:
        """重建到第 ``number`` 帧为止；同一时间戳的后续帧不会混进来。"""
        self.reset()
        last = 0.0
        for frame in self.recording.frames():
            if frame.number > number:
                break
            self.apply(frame)
            last = frame.at
        self._position = last
