"""内置命令 —— 按类别分模块，导入即完成注册。

命令是**完整处理流程**：自己通过 ``Services`` 完成全部行为。
只有「切输入处理器」和「退出程序」通过 ``CommandResult`` 返回。

新增命令只需在这里加一行导入；其余一切由装饰器完成。
"""

from __future__ import annotations

from . import agent, core, generate, write  # noqa: F401

__all__ = ["agent", "core", "generate", "write"]
