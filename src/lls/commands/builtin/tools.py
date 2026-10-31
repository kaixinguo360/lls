"""tools —— 全局工具开关（Ctrl-E 命令层）。

与 agent 内 ``/tools`` 的分工：

* 这里改的是**全局**（进程级）策略，影响之后打开、以及切换过去的 agent
  会话；已经在跑的会话下一轮请求也会用它（每轮现算）。
* agent 内的开关改的是当前会话层，只影响那一条会话。

两层逐字段合并，会话层更近。全局修改只对当前进程有效：写回 ``config.yaml``
需要保留注释与格式，那是另一件事；要持久化就手改配置里的 ``ai.tools``。
"""

from __future__ import annotations

from lls.ai.tools import builtin_tools
from lls.ai.tools.policy import global_policy
from lls.commands.result import CommandResult
from lls.commands.spec import command
from lls.session.services import Services

from .tool_picker import toggle_tools


def tool_names(services: Services) -> list[str]:
    """默认工具集的名字，顺序与请求里的 ``tools`` 数组一致。"""
    return [entry.tool.name for entry in builtin_tools(services, services.wait)]


def apply_global_states(states: dict[str, bool]) -> None:
    """把界面结果写进全局策略。"""
    policy = global_policy()
    for name, wanted in states.items():
        if wanted:
            policy.enable(name)
        else:
            policy.disable(name)


@command("tools", category="ai")
async def cmd_tools(s: Services, args: str | None) -> CommandResult:
    """打开全局工具开关（影响之后的 agent 会话）"""
    names = tool_names(s)
    if not names:
        await s.show_message("没有可用工具")
        return CommandResult()
    states = {name: global_policy().enabled(name) for name in names}
    chosen = await toggle_tools(
        s,
        names,
        states,
        header="全局工具开关（之后的 agent 会话都按它算；本次进程有效）",
    )
    if chosen is None:
        return CommandResult()
    apply_global_states(chosen)
    off = [name for name in names if not chosen[name]]
    if off:
        await s.show_message("全局工具已更新（本次进程有效）：关闭 " + "、".join(off))
    else:
        await s.show_message("全局工具已更新（本次进程有效）：全部启用")
    return CommandResult()
