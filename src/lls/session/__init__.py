"""session 包：终端引擎与它对外暴露的能力。

**本包不依赖命令层。** 引擎只提供原语（PTY、屏幕模型、渲染、输入、信号），
由外部注册的处理器决定如何响应输入。
"""

from .engine import Engine
from .services import Handler, Services

__all__ = ["Engine", "Handler", "Services"]
