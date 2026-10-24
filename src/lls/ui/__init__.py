"""ui 包：显示原语（精确移植 legacy display.py / terminal.py）。"""

from .renderer import (
    LinePrinter,
    char_width,
    content_start,
    full_redraw,
    print_screen_perfect,
    render_row_cells,
    wrap_multi_lines,
)

__all__ = [
    "LinePrinter",
    "char_width",
    "content_start",
    "full_redraw",
    "print_screen_perfect",
    "render_row_cells",
    "wrap_multi_lines",
]
