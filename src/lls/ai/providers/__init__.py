"""provider 实现目录。

一个实现一个模块，导入即向 :mod:`lls.ai.registry` 注册自己的 ``type``。
新增后端只需在这里加一个模块并注册，上层代码不需要改动。

两种实现说同一种协议，区别只在传输层：

* ``openai``      —— 用 ``openai`` 包，省事，但多一个依赖；
* ``openai-http`` —— 只用标准库直接发 HTTP/1.1，不装 SDK 也能用。
"""

from __future__ import annotations

from ..registry import register_type
from .http import HttpProvider
from .openai import OpenAIProvider

__all__ = ["HttpProvider", "OpenAIProvider"]

register_type("openai", OpenAIProvider)
register_type("openai-http", HttpProvider)
