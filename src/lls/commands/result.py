"""命令返回值。

命令是完整流程：清屏、读输入、写子进程、画覆盖层都在命令内部通过引擎的
``Services`` 完成。唯一命令做不到的事是**改变引擎状态**——切换输入处理器，
因为那是引擎主循环的职责。所以这里只有一项请求。

这份返回值刻意保持极小：它每多一项，框架对「命令能做什么」的预设就多一条，
能被表达的行为反而少一分。
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["CommandResult"]


@dataclass(frozen=True)
class CommandResult:
    """命令执行完毕后的状态请求。默认什么都不请求。"""

    handler: str | None = None
    """请求切换到的输入处理器；None 表示保持当前处理器。

    名字由应用层的处理器注册表定义，引擎不认识它的含义，只按名字查找。
    """
