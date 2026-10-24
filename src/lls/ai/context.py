"""Context —— 无损原料。

一次生成发生时终端现场的完整快照。这是本项目「尽可能保留原始输入」的落点，
也是唯一不能妥协的部分：只读、不可变、不裁剪。

截断、去 ANSI、抽样式都是 Role 的策略，不是 Context 的职责。
"""

from __future__ import annotations

from dataclasses import dataclass

from lls.term.model import Cell, ScreenModel

__all__ = ["Context"]


@dataclass(frozen=True, slots=True)
class Context:
    text: str                                   # 可见区纯文本（已 rstrip）
    scrollback: str                              # 回滚区文本
    raw: bytes                                   # 原始字节流（含全部 ANSI）
    cells: tuple[tuple[Cell, ...], ...]          # 逐格网格（含样式）
    cwd: str | None = None                       # OSC 7 采集
    shell: str | None = None
    cursor: tuple[int, int] = (0, 0)
    last_command: str | None = None              # OSC 133 采集

    @classmethod
    def from_screen(
        cls,
        screen: ScreenModel,
        *,
        raw: bytes = b"",
        cwd: str | None = None,
        shell: str | None = None,
        last_command: str | None = None,
    ) -> "Context":
        """从屏幕模型采样原料。"""
        cursor = screen.cursor
        return cls(
            text="\n".join(screen.lines()),
            scrollback="\n".join(screen.scrollback()),
            raw=raw,
            cells=tuple(tuple(screen.row_cells(r)) for r in range(screen.rows)),
            cwd=cwd,
            shell=shell,
            cursor=(cursor.x, cursor.y),
            last_command=last_command,
        )
