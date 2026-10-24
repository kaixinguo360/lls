"""命令上下文：命令执行环境。


设计原则（与「命令即完整流程」一致）：

* 命令是**完整的处理流程**，自己通过 ``ctx`` 提供的能力直接完成行为
  （清屏、显示屏幕、读输入、写子进程、显示消息），不再把自身行为
  包装成"副作用描述"交给外部解释——那会把一个命令拆成两半，各自为政。
* ``CommandContext`` 只提供**窄能力接口**（``ui`` / ``io``），由应用层
  注入具体实现；因此命令仍可脱离真实终端测试（注入 fake）。
* 只有命令**做不了**的事（切输入处理器、退出程序）才通过
  :class:`~lls.commands.result.CommandResult` 返回。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from lls.ai import AIConfig, Context, Role, Session
from lls.term import ScreenModel

__all__ = ["CommandContext", "ShellService", "UIService"]


class UIService(Protocol):
    """宿主终端的显示能力（由应用层注入，引擎的 Services 直接满足）。

    这些是命令的**实际行为**，不是"后效应"——``clear`` 命令的全部作用就是
    调用 ``clear_screen()``，没必要再绕一圈用 Effect 描述。
    """

    def clear_screen(self) -> None:
        """清屏并归位。"""
        ...

    def show_screen(self, *, raw: bool = False) -> None:
        """清屏后以边框视图显示屏幕内容；``raw=True`` 时显示原始字节。"""
        ...

    async def show_message(self, text: str) -> None:
        """以覆盖层显示消息，等任意键后收起。不改变当前处理器。"""
        ...

    def restore_context(self) -> None:
        """收起覆盖层，按模型还原它占的行，并把物理光标定位到模型光标。"""
        ...

    def draw_overlay(self, text: str, cursor: int | None = None) -> tuple[int, int]:
        """在模型光标处画覆盖层，返回 ``(起始行, 行数)``。不写模型。"""
        ...

    def write_terminal(self, data: bytes) -> None:
        """直接写宿主终端（透传输出或自绘界面用）。"""
        ...


class ShellService(Protocol):
    """子进程读写能力（由应用层注入，引擎的 Services 直接满足）。"""

    def write(self, data: bytes, *, enter: bool = False) -> None:
        """向子进程写字节；``enter`` 为 True 时追加换行。"""
        ...

    async def read_line(
        self,
        prompt: str,
        *,
        include_last: bool = False,
        value: str = "",
        cancel: str | None = "",
        exit: str | None = None,
        history_id: str = "cmd",
        escape: object | None = None,
    ) -> object:
        """读一行输入（完整行编辑）。Ctrl-C 返回 ``cancel`` 值。"""
        ...

    async def read_key(self) -> object:
        """读一个按键；子进程退出时返回 None。"""
        ...

    async def read_chars(self, timeout: float | None = None) -> bytes | None:
        """读一段原始输入字节（不解析按键）。"""
        ...

    def record_history(self, value: str, history_id: str) -> None:
        """把一条输入记入某历史列表。"""
        ...

    @property
    def child_alive(self) -> bool:
        """子进程是否仍在运行。"""
        ...

    def flush_model(self) -> None:
        """把尚未解析的子进程输出全部喂给模型（透传模式下模型在后追赶）。"""
        ...


@dataclass(slots=True)
class CommandContext:
    """命令执行环境：只读数据 + 窄能力接口。

    ``role_id`` 是唯一可写的字段：``mode`` 命令要能切换当前角色。其余数据
    由应用层在构造时注入，命令不应改写（它们属于配置，持久化在 TOML 里）。
    """

    # 能力（由应用层注入；引擎的 Services 直接满足这两个协议）
    ui: UIService
    io: ShellService

    # 可写：当前角色（mode 命令会改）
    role_id: str

    # 只读数据
    screen: ScreenModel
    ai_config: AIConfig
    roles: dict[str, Role]
    session: Session

    # 原料采集参数（由应用层旁路维护）
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
