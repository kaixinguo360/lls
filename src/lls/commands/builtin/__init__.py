"""内置命令 —— 按类别分模块，导入即完成注册。

命令是**完整处理流程**：自己通过 ``ctx.ui`` / ``ctx.io`` 完成全部行为
（清屏、显示、读输入、写子进程）。只有「切输入处理器」和「退出程序」通过
``CommandResult`` 返回 —— 那两件是引擎状态机的一部分。

命令名与别名对照 legacy：

    core      q/quit/exit, s/show/status, r/raw, ch/chat, reset, c/clear,
              err, conf/config/configs, m/mode, l/ls, get
    terminal  w/watch, t/tty
    write     e/exec, i/input
    generate  g/gen/generate, a/auto

新增命令只需在这里加一行导入；其余一切由装饰器完成。
"""

from __future__ import annotations

from . import core, generate, terminal, write  # noqa: F401

__all__ = ["core", "generate", "terminal", "write"]
