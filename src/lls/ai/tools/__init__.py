"""工具：给 agent 调用的一组能力，与具体命令、会话、界面无关。

这里只回答两个问题：**有哪些工具**，以及**它们怎么被套起来**。

* 一个工具 = 协议定义 + 执行函数（:class:`lls.ai.AgentTool`）。协议是给模型看
  的说明书（名字、参数、JSON Schema），执行函数做实事并回一段观察文本。
* 所有工具共有的行为（连续相同调用的提醒，将来还会有审计日志、写操作确认）
  是套在外面的中间件，见 :class:`~lls.ai.tools.box.Toolbox`。
* 工具依赖的东西以窄接口表达：终端是 :class:`~lls.ai.tools.terminal.TerminalPort`
  （引擎的 ``Services`` 结构上就满足），文件是文件系统，shell 是一个
  :class:`~lls.ai.tools.shell.Shell` 实例。所以本包不 import ``lls.session``
  与 ``lls.commands`` —— 换一个宿主也能用同一组工具。

扩展点因此有三个，互不牵动：

1. 新工具 → 在对应模块里写 ``Tool`` + 执行函数 + 一个 ``xxx_tools()``；
2. 新的一族 → 新模块，同一条约定（返回 ``list[AgentTool]``）；
3. 新的横切行为 → 一个中间件，装配时 ``add_middleware`` 一行。

``builtin_tools`` 把默认的那一套装好；要裁剪或替换，自己 ``Toolbox`` 拼即可。
"""

from __future__ import annotations

from .box import Middleware, Next, Run, Toolbox
from .files import file_tools
from .middleware import RepeatReminder
from .policy import ToolPolicy, global_policy, reset_global_policy
from .shell import Shell, shell_tools, shared
from .terminal import Poll, ScreenView, TerminalPort, terminal_tools

__all__ = [
    "Middleware",
    "Next",
    "Poll",
    "RepeatReminder",
    "Run",
    "ScreenView",
    "Shell",
    "TerminalPort",
    "ToolPolicy",
    "Toolbox",
    "builtin_tools",
    "global_policy",
    "reset_global_policy",
    "file_tools",
    "shell_tools",
    "terminal_tools",
]


def builtin_tools(
    port: TerminalPort,
    poll: Poll,
    *,
    view: ScreenView | None = None,
    shell: Shell | None = None,
) -> Toolbox:
    """装好默认那八个工具。

    ``view`` 传进来时与宿主共用同一份「上次读到的画面」：任务开头拍的那张
    快照也算看过，模型第一次读屏不会白读一遍重复内容。``shell`` 不给就用
    进程共用的那个（``cd`` 的位置跨会话保留）。

    返回的是**没套中间件**的工具箱，中间件由调用方按需加 —— 谁用谁知道要不要
    「连续相同调用」的提醒。
    """
    view = view if view is not None else ScreenView(port, poll)
    box = Toolbox()
    box.extend(terminal_tools(port, view))
    box.extend(file_tools())
    box.extend(shell_tools(shell if shell is not None else shared()))
    return box
