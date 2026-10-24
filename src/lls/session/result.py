"""命令返回值 —— 只承载「命令自己做不了」的事。

这里刻意只保留两项：

* ``mode``：请求切换主循环模式（char/line/prompt）。模式是 controller 的状态机，
  命令无法自己修改，只能请求。
* ``quit``：请求退出 LLS。

其余一切（清屏、显示屏幕、读输入、写子进程、显示消息）都在命令内部通过
``CommandContext`` 的能力接口完成 —— 那些是命令的**行为本身**，不是后效应。

（早期版本把它们全部抽象成 Effect 列表，导致一个命令的完整流程被拆成
「命令返回描述」+「controller 解释描述」两半，改行为要同时动两处。已废弃。）
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["CommandResult"]


@dataclass(frozen=True, slots=True)
class CommandResult:
    """命令执行完毕后的状态请求。默认什么都不请求。"""

    mode: str | None = None
    """请求切换到的模式；None 表示保持当前模式。"""

    quit: bool = False
    """请求退出 LLS。"""
