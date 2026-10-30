"""输入层的中断语义。

引擎只认识「键盘上发生了什么」，不认识命令、模型这些概念，因此这里只放
两个最朴素的结果：用户在等的时候按了 Ctrl-C，或者子进程已经结束。

上层（命令层）把它们翻成自己的中断异常；AI 层则完全不认识这两个名字。
"""

from __future__ import annotations

__all__ = ["Interrupted", "SessionEnded"]


class Interrupted(Exception):
    """等输入期间用户按了 Ctrl-C：当前这段等待被放弃，调用方据此取消在等的事。"""


class SessionEnded(Exception):
    """子进程退出（stdin 关掉了）：会话无法再继续。"""
