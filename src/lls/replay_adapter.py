"""回放层与 LLS 终端模型的唯一接缝。

录制包不认识 ``lls.term`` / ``lls.ui``；LLS 入口把这两个函数注入 ``recording.cli``，
回放因此和实时画面共用同一套备用屏、宽度与颜色规则。
"""

from __future__ import annotations

from typing import Any, Sequence

from .term import PyteScreenModel
from .ui.renderer import render_row_cells

__all__ = ["row_renderer", "screen_factory"]


def screen_factory(rows: int, columns: int) -> PyteScreenModel:
    return PyteScreenModel(rows=rows, columns=columns)


def row_renderer(cells: Sequence[Any], keep_trailing_blank: bool = False) -> str:
    return render_row_cells(cells, keep_trailing_blank)
