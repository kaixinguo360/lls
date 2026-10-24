"""命令返回值 —— 只承载「命令自己做不了」的事。

只有一项：``mode``，请求切换主循环模式（char/line/prompt）。模式属于
controller 的状态机，命令无法自己修改，只能请求。

其余一切（清屏、显示屏幕、读输入、写子进程、显示消息）都在命令内部通过
``CommandContext`` 的能力接口完成 —— 那些是命令的**行为本身**，不是后效应。
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["CommandResult"]


@dataclass(frozen=True, slots=True)
class CommandResult:
    """命令执行完毕后的状态请求。默认什么都不请求。"""

    mode: str | None = None
    """请求切换到的模式；None 表示保持当前模式。"""
