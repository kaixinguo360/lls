"""ui 包：显示原语（移植自 legacy display.py / terminal.py）。"""

from .renderer import (
    LinePrinter,
    char_width,
    content_start,
    overlay_anchor,
    print_screen_perfect,
    render_row_cells,
    wrap_multi_lines,
)

__all__ = [
    "LinePrinter",
    "char_width",
    "content_start",
    "overlay_anchor",
    "print_screen_perfect",
    "render_row_cells",
    "wrap_multi_lines",
]
