"""命令返回值 —— 只承载「命令自己做不到」的事。

命令是完整流程：清屏、显示、读输入、写子进程、画覆盖层都在命令内部通过
``CommandContext`` 完成。只有**改变引擎状态**这两件事命令无法自己做，
必须通过本类提出请求：

* ``handler``：请求切换到另一个输入处理器。名字由应用层的处理器注册表定义，
  引擎不认识它的含义，只按名字查找；
* ``quit``：请求退出整个程序。

这份返回值刻意保持极小：它越长，框架对「命令能做什么」的预设就越多，
能被表达的行为反而越少。
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["CommandResult"]


@dataclass(frozen=True, slots=True)
class CommandResult:
    """命令执行完毕后的状态请求。默认什么都不请求。"""

    handler: str | None = None
    """请求切换到的输入处理器；None 表示保持当前处理器。"""

    quit: bool = False
    """请求退出整个程序。"""
