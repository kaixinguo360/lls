"""一级子命令的注册形状。每个命令自带 ``run(argv)`` 和参数解析，主入口只负责
在第一个位置参数处做一次分派。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

__all__ = ["Command"]


@dataclass(frozen=True)
class Command:
    name: str
    summary: str
    run: Callable[[list[str]], int]
