"""学习相关的旁路能力：教师求助、配置与离线入口。

这里不碰 ``Agent`` 主循环，也不依赖终端或命令层；``learn`` 只提供
``ask_teacher`` 工具所需的教师实现、预算与脱敏，以及 ``lls-learn`` 的
自检入口。
"""

from __future__ import annotations

__all__ = ["config", "help", "redact", "teacher"]
