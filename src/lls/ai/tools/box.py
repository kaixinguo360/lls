"""工具箱：装工具，并给所有工具统一套一层中间件。

一个工具就是「协议定义 + 执行函数」（:class:`~lls.ai.AgentTool`）。凡是**每个
工具都要做**的事 —— 连续相同调用的提醒、以后的审计日志、写操作前的确认、
计时 —— 都不该在每个实现里各写一遍 ``guard.after(...)``：那样加一个新工具就
得记得再写一遍，漏了也没人发现。这类行为是套在外面的**一层**，而不是工具
本身的一部分::

    box = Toolbox()
    box.add(READ_TOOL, read_terminal)
    box.add(WRITE_FILE_TOOL, write_file)
    box.add_middleware(RepeatReminder())
    Agent(provider, model, system=..., tools=box)

中间件是 ``(name, args, call) -> str`` 的协程：``call()`` 交回后面的链，所以
它可以在前后做事，也可以**根本不调用它** —— 拒绝一次调用（用户没批准写文件）
用不着改动工具实现。``args`` 是解析后的参数 dict，只看不改。

两层扩展因此互不牵动：

* 加一个工具 = 写一个实现 + :meth:`Toolbox.add` 一行；
* 加一种横切行为 = 写一个中间件 + :meth:`Toolbox.add_middleware` 一行。

``Toolbox`` 本身可迭代出 :class:`~lls.ai.AgentTool`（中间件已经套好），所以能
直接交给 :class:`~lls.ai.Agent`，也能直接拿来列 ``/tools``。
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, Iterable, Iterator

from ..agent import AgentTool
from ..types import Tool

__all__ = ["Middleware", "Next", "Toolbox", "Run"]

#: 交回下一层（或最终的工具实现）并拿到它的观察文本。
Next = Callable[[], Awaitable[str]]

#: 一个执行函数：收参数，返回给模型看的观察文本。
Run = Callable[[Dict[str, Any]], Awaitable[str]]

#: 中间件：``(工具名, 参数, 下一层) -> 观察文本``。
Middleware = Callable[[str, Dict[str, Any], Next], Awaitable[str]]


def _bind(layer: Middleware, name: str, inner: Run) -> Run:
    """把一层中间件套在 ``inner`` 外面。"""

    async def run(arguments: Dict[str, Any]) -> str:
        return await layer(name, arguments, lambda: inner(arguments))

    return run


class Toolbox:
    """一组工具 + 一层中间件。先装配，再取用。"""

    def __init__(self, tools: Iterable[AgentTool] = ()) -> None:
        self._tools: list[AgentTool] = list(tools)
        self._middleware: list[Middleware] = []

    # ---- 装 ----

    def add(self, tool: Tool, run: Run) -> None:
        """加一个工具。"""
        self._tools.append(AgentTool(tool, run))

    def extend(self, tools: Iterable[AgentTool]) -> None:
        """把另一份（模块给出的一份工具）原样并进来。"""
        self._tools.extend(tools)

    def add_middleware(self, *middleware: Middleware) -> None:
        """追加中间件，先加的在外层：它先看到调用，也最后看到结果。"""
        self._middleware.extend(middleware)

    # ---- 用 ----

    def names(self) -> list[str]:
        """工具名，顺序与请求里给出的一致。"""
        return [entry.tool.name for entry in self._tools]

    def __len__(self) -> int:
        return len(self._tools)

    def __iter__(self) -> Iterator[AgentTool]:
        """套好中间件的工具，可直接交给 :class:`~lls.ai.Agent`。

        每次迭代现套：中间件的状态在它自己身上（真实状态），这里只是把链
        重新串一遍，迭代多次结果一致。
        """
        for entry in self._tools:
            run = entry.run
            for layer in reversed(self._middleware):
                run = _bind(layer, entry.tool.name, run)
            yield AgentTool(entry.tool, run)
