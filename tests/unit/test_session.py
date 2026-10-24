"""覆盖层几何与恢复策略的单元测试。

回归背景：

* 提示符折行占多行时，若只恢复光标所在的一行，下方会留下空白；
* 覆盖层画到屏幕底之外会让终端滚动、内容被顶走，物理画面与屏幕模型随之错位，
  之后子进程的回显会被写进模型里错误的位置（实测表现为一行内容后面被追加
  一整串命令文本）；
* 退出覆盖层时若只还原行、把列归零，光标会落回行首
  （实测 exec vim 退出后从 (1,5) 变成 (1,1)）。

设计说明：覆盖层（命令行、消息、生成预览）一律**从行首开始画**，
不跟随子进程光标的列 —— 命令行左对齐才符合阅读习惯（同 tmux 状态行、
readline 提示符）。跟随光标列是曾考虑过但未采用的方案。
"""

from __future__ import annotations

from lls.session.controller import clamp_cursor, needs_full_redraw
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

    这是「覆盖层把终端撑得滚动」的核心回归。
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


# ---- needs_full_redraw：退出覆盖层后的恢复方式 ----

def test_single_line_untouched_uses_light_restore() -> None:
    """只覆盖一行且期间无输出：走轻量单行恢复（无闪烁）。"""
    assert needs_full_redraw(model_changed=False, covered_lines=1) is False


def test_model_changed_forces_full_redraw() -> None:
    """覆盖期间子进程有输出：模型领先，必须整屏重绘。"""
    assert needs_full_redraw(model_changed=True, covered_lines=1) is True


def test_multiline_overlay_forces_full_redraw() -> None:
    """覆盖层占多行：被擦掉的行必须按模型恢复。"""
    assert needs_full_redraw(model_changed=False, covered_lines=2) is True
    assert needs_full_redraw(model_changed=False, covered_lines=3) is True


def test_both_conditions() -> None:
    assert needs_full_redraw(model_changed=True, covered_lines=5) is True


# ---- clamp_cursor：退出覆盖层时还原光标位置（行 + 列）----
#
# 回归背景：早期只记录「行」并还原到行首，导致恢复后光标总在第 1 列。
# 两类场景都会踩到：
#   * shell 提示符带尾随空格，光标原先停在提示符之后（如第 8 列）；
#   * exec 启动全屏程序（vim），它的光标停在第 5 列。
# 只回行首就与离开时不一致 —— 表现为「从 line 恢复后光标位置不对」。

def test_clamp_keeps_valid_position() -> None:
    """合法位置原样返回（列不能被压成 0）。"""
    assert clamp_cursor(4, 7, rows=16, columns=50) == (4, 7)
    assert clamp_cursor(0, 0, rows=16, columns=50) == (0, 0)


def test_clamp_limits_to_screen_edges() -> None:
    """越界位置（如覆盖期间终端缩小）钳制到边界。"""
    assert clamp_cursor(100, 200, rows=16, columns=50) == (15, 49)
    assert clamp_cursor(-5, -3, rows=16, columns=50) == (0, 0)


def test_clamp_preserves_column_not_just_row() -> None:
    """列必须保留 —— 这正是「光标跳到第 1 列」的回归点。"""
    row, col = clamp_cursor(4, 19, rows=16, columns=50)
    assert col == 19, "列被重置会让光标跳到行首"


def test_clamp_vim_style_position() -> None:
    """exec vim 退出场景：光标应在 "  1" 之后的第 5 列，而不是行首。"""
    row, col = clamp_cursor(0, 4, rows=20, columns=60)
    assert (row, col) == (0, 4)
    assert col + 1 == 5, "1-based 列号应为 5"
