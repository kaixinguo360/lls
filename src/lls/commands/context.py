"""命令上下文：命令执行环境。


设计原则（与「命令即完整流程」一致）：

* 命令是**完整的处理流程**，自己通过 ``ctx`` 提供的能力直接完成行为
  （清屏、显示屏幕、读输入、写子进程、显示消息），不再把自身行为
  包装成"副作用描述"交给外部解释——那会把一个命令拆成两半，各自为政。
* ``CommandContext`` 只提供**窄能力接口**（``ui`` / ``io``），由 controller
  注入具体实现；因此命令仍可脱离真实终端测试（注入 fake）。
* 只有命令**做不了**的事（改 controller 的模式状态机、退出程序）才通过
  :class:`~lls.session.result.CommandResult` 返回。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from lls.ai import AIConfig, Context, Role, Session
from lls.term import ScreenModel

__all__ = ["CommandContext", "ShellService", "UIService"]


class UIService(Protocol):
    """宿主终端的显示能力（由 controller 实现）。

    这些是命令的**实际行为**，不是"后效应"——``clear`` 命令的全部作用就是
    调用 ``clear_screen()``，没必要再绕一圈用 Effect 描述。
    """

    def clear_screen(self) -> None:
        """清屏（legacy cmd_clear）。"""
        ...

    def clear_screen_line(self) -> None:
        """清除当前行（legacy ``print('\\033[2K\\r')``）。"""
        ...

    def show_screen(self, *, raw: bool = False) -> None:
        """清屏并以边框视图显示当前屏幕（legacy cmd_show / cmd_raw）。"""
        ...

    async def show_message(self, text: str) -> None:
        """显示消息，等任意键后清除（legacy show_line，不改变模式）。"""
        ...

    def restore_context(self) -> None:
        """恢复被覆盖模式占用的画面（legacy print_context）。"""
        ...


class ShellService(Protocol):
    """子进程读写能力（由 controller 实现）。"""

    def write(self, data: bytes, *, enter: bool = False) -> None:
        """向子进程写字节；``enter`` 为 True 时追加换行。"""
        ...

    async def read_line(
        self,
        prompt: str,
        *,
        history_id: str = "cmd",
        cancel: str | None = "",
    ) -> str | None:
        """读一行输入（完整行编辑）；Ctrl-C 返回 ``cancel`` 值。"""
        ...


@dataclass(slots=True)
class CommandContext:
    """命令执行环境：只读数据 + 窄能力接口。"""

    # 能力（由 controller 注入）
    ui: UIService
    io: ShellService

    # 只读数据
    screen: ScreenModel
    ai_config: AIConfig
    roles: dict[str, Role]
    role_id: str
    session: Session

    # 原料采集参数（由 controller 旁路维护）
    raw: bytes = b""
    cwd: str | None = None
    shell: str | None = None
    last_command: str | None = None

    def make_context(self) -> Context:
        return Context.from_screen(
            self.screen,
            raw=self.raw,
            cwd=self.cwd,
            shell=self.shell,
            last_command=self.last_command,
        )
