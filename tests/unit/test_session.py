"""覆盖层几何的单元测试。

架构：**两个屏幕**

* 模型 = 子进程的终端画面（唯一真相，含真实光标）；
* 覆盖层 = 本程序的浮动层，画在物理终端上，**不写模型**。

由此得出的关键不变量（本文件的测试都在守护它们）：

1. 覆盖层绝不能越过屏幕边界 —— 一旦越界，终端滚动，物理行与模型行错位；
2. 覆盖层只清空它自己的行区间，恢复时按模型重绘这些行即可，
   不需要判断「整屏重绘还是单行恢复」。
"""

from __future__ import annotations

from lls.ui import overlay_anchor

# ---- overlay_anchor：覆盖层必须自己约束在屏幕内 ----

def test_overlay_fits_below_cursor() -> None:
    """空间足够：直接覆盖模型光标所在行。"""
    assert overlay_anchor(5, 1, 20) == 5
    assert overlay_anchor(5, 3, 20) == 5


def test_overlay_shifts_up_to_stay_inside_screen() -> None:
    """放不下时整体上移：末行对齐屏幕底，绝不越界。

    20 行终端、光标已在最底行（19，0-based）、需要 3 行 -> 起始行 17。
    """
    assert overlay_anchor(19, 3, 20) == 17
    assert overlay_anchor(19, 1, 20) == 19


def test_overlay_never_exceeds_bottom() -> None:
    """穷举：任意光标行与内容高度都不越下边界。

    覆盖层把终端撑得滚动，物理行与模型行就会错位 —— 这里守住那条边界。
    """
    rows = 20
    for cursor_y in range(rows):
        for lines_needed in range(1, rows + 1):
            row = overlay_anchor(cursor_y, lines_needed, rows)
            assert row >= 0
            assert row + lines_needed <= rows


def test_overlay_taller_than_screen_sticks_to_top() -> None:
    """覆盖层比屏幕还高：贴顶（内容无法完整显示，但不滚动）。"""
    assert overlay_anchor(10, 25, 20) == 0


def test_overlay_never_scrolls_at_any_position() -> None:
    """只要不越界，终端就不会滚动 —— 物理行恒等于模型行。

    这是「按行重绘即可恢复」这条简化路径的前提，必须始终成立。
    若某天新增了「覆盖层占满全屏」之类的策略，这个测试会先失败。
    """
    rows, _columns = 24, 80
    for cursor_y in range(rows):
        for lines_needed in range(1, rows + 1):
            top = overlay_anchor(cursor_y, lines_needed, rows)
            bottom = top + lines_needed
            assert 0 <= top <= bottom <= rows
