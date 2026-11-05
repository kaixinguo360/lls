"""录制读取失败的类型。写失败不在这里：录制是旁路，写端由引擎就地兜住。"""

from __future__ import annotations

__all__ = [
    "FrameUnavailable",
    "RecordingCorrupt",
    "RecordingDeleted",
    "RecordingError",
    "RecordingNotFound",
]


class RecordingError(Exception):
    """录制层的基类；CLI 用它区分「数据问题」和「程序错误」。"""


class RecordingNotFound(RecordingError):
    """目录不存在，且没有对应的删除记录。"""

    def __init__(self, path: object) -> None:
        super().__init__(f"找不到录制：{path}")


class RecordingDeleted(RecordingError):
    """录制曾被总量上限删除；墓碑里留了 id 和删除时间。"""

    def __init__(self, recording_id: str, deleted_at: float | None = None) -> None:
        detail = f"（删除于 {deleted_at:.0f}）" if deleted_at else ""
        super().__init__(f"录制已被删除：{recording_id}{detail}")


class RecordingCorrupt(RecordingError):
    """元数据或分段不满足当前格式。"""

    def __init__(self, path: object, detail: str) -> None:
        super().__init__(f"录制损坏：{path}（{detail}）")


class FrameUnavailable(RecordingError):
    """请求的帧不在当前保留窗口内。"""

    def __init__(self, number: int, earliest: int) -> None:
        super().__init__(
            f"第 {number} 帧不可用；当前保留范围从第 {earliest} 帧开始"
        )
