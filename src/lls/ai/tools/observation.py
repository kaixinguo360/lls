"""工具观察的运行结果：一段文本 + 这次调用的事实。

文本是模型看到的那份，也是写进会话事件、之后原样重放的那份；``data`` 只放
**从文本里不好可靠取回来**的事实 —— 退出码、光标位置、行范围、耗时。正文
（屏幕、stdout、文件内容）不进 ``data``：它已经在文本里，再存一份就是双倍体积。

为什么是 ``str`` 的子类而不是新的返回类型：中间件（``(name, args, call) ->
str``）、断言、拼接全都按字符串工作，换类型等于把所有调用点改一遍。子类让
"还是字符串"成立，同时把事实挂在对象上带走。
"""

from __future__ import annotations

from typing import Any, Mapping

__all__ = ["Observation"]


class Observation(str):
    """工具的返回值：``str`` 相等、可拼接，另带一份结构化事实。"""

    data: Mapping[str, Any]

    def __new__(
        cls, text: str, data: Mapping[str, Any] | None = None
    ) -> "Observation":
        value = super().__new__(cls, text)
        value.data = dict(data or {}) if data else {}
        return value
