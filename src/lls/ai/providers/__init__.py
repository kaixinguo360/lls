"""provider 实现目录。

一个实现一个模块，导入即向 :mod:`lls.ai.registry` 注册自己的 ``type``。
新增后端只需在这里加一个模块并注册，上层代码不需要改动。
"""

from __future__ import annotations

from ..registry import register_type
from .openai import OpenAIProvider

__all__ = ["OpenAIProvider"]

register_type("openai", OpenAIProvider)
