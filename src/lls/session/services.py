"""Services —— 框架对外提供的全部能力。

这是「终端引擎」与「上层逻辑」之间**唯一**的接口。引擎本身不认识命令、
处理器这些概念；它只负责 PTY、屏幕模型、渲染与输入，并通过本类把这些
能力暴露出去。处理器与命令都只依赖这里的原语，因而可以自由组合出任意行为，
无需改动引擎。

约定：
* 所有方法都是**原语**，不做业务判断（不解析命令、不决定处理器）；
* 会阻塞等待用户输入的方法都是 ``async``；
* 引擎状态只有一个所有者（引擎自身），本类只做转发，不缓存。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from lls.term import ScreenModel

if TYPE_CHECKING:  # pragma: no cover
    from .engine import Engine

__all__ = ["Handler", "Services"]


class Handler(Protocol):
    """输入处理器。

    引擎的主循环做的唯一一件事就是反复调用当前处理器的 :meth:`step`。
    处理器切换自己由 :meth:`Services.set_handler` 请求，引擎不认识
    ``char`` / ``line`` 之类的名字 —— 它们只是应用层注册表里的键。

    ``passthrough`` 属性声明「子进程输出是否原样透传给终端」：
    透传时显示优先、模型在后追赶；否则终端内容一律由模型投影而来。
    """

    passthrough: bool

    async def step(self, services: "Services") -> None:
        """处理一轮输入。可以长时间等待，也可以立即返回。"""
        ...


class Services:
    """转发到引擎的原语集合。"""

    __slots__ = ("_engine",)

    def __init__(self, engine: "Engine") -> None:
        self._engine = engine

    # ---- 屏幕模型 ----

    @property
    def screen(self) -> ScreenModel:
        """子进程的屏幕模型（唯一真相）。"""
        return self._engine.screen

    # ---- 宿主终端显示 ----

    def clear_screen(self) -> None:
        """清屏并归位（``ESC[2J ESC[H``）。"""
        self._engine.clear_screen()

    async def show_message(self, text: str) -> None:
        """以覆盖层显示一段文本，等任意键后收起。不改变当前处理器。"""
        await self._engine.show_message(text)

    def restore_context(self) -> None:
        """收起覆盖层，按模型还原它占的行，并把物理光标定位到模型光标。"""
        self._engine.restore()

    # ---- 覆盖层（更细的原语，供需要自绘界面的处理器使用）----

    def draw_overlay(self, text: str, cursor: int | None = None) -> tuple[int, int]:
        """在模型光标处画覆盖层，返回 ``(起始行, 行数)``。不写模型。"""
        return self._engine.draw_overlay(text, cursor)

    # ---- 子进程 ----

    def write(self, data: bytes, *, enter: bool = False) -> None:
        """向子进程写字节；``enter=True`` 时追加换行。"""
        self._engine.write_child(data, enter=enter)

    # ---- 输入 ----

    async def read_chars(self, timeout: float | None = None) -> bytes | None:
        """读一段原始输入字节（不解析按键）。超时返回 ``b""``，退出返回 None。"""
        return await self._engine.read_chars(timeout)

    async def read_line(
        self,
        prompt: str = ":",
        *,
        include_last: bool = False,
        value: str = "",
        cancel: str | None = None,
        exit: str | None = None,
        history_id: str = "_default",
        escape: Any = None,
    ) -> Any:
        """读一行（完整行编辑）。

        返回：输入内容；Ctrl-C → ``cancel``；Ctrl-D → ``exit``；
        Esc 且 ``escape`` 非 None → ``escape``；子进程退出 → None。
        """
        return await self._engine.read_line(
            prompt,
            include_last=include_last,
            value=value,
            cancel=cancel,
            exit=exit,
            history_id=history_id,
            escape=escape,
        )

    def push_keys(self, data: bytes) -> None:
        """把字节回填到输入队列（切换处理器时避免丢键）。"""
        self._engine.push_keys(data)

    def requeue_pending_keys(self) -> None:
        """把已解析但尚未消费的按键回填到输入队列。

        退出覆盖模式（键盘层）转去透传（原始字节层）时必须调用：两层读的是
        不同队列，留在键盘缓冲里的键不会自动被透传层看到。
        """
        self._engine.requeue_pending_keys()

    # ---- 模型同步 ----

    def flush_model(self) -> None:
        """把尚未解析的子进程输出全部喂给模型。

        透传模式下模型在后追赶，任何要读模型状态的地方都应先调用本方法；
        已同步时是空操作。
        """
        self._engine.flush_backlog()

    # ---- 处理器切换 ----

    def set_handler(self, name: str) -> None:
        """请求引擎在下一轮切换到 ``name`` 处理器。

        引擎不认识这些名字，只按注册表查找；切换在下一次循环迭代生效。
        这样切换是**协作式**的：处理器总能把当前这一步做完。
        """
        self._engine.request_handler(name)
