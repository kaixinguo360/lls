"""跨工具通用的中间件：现在只有「连续相同调用」的提醒。

放在这里而不是放进各工具里，是因为这条规则对**所有**工具都成立：模型卡在同一个
``read_terminal`` 上空转，和卡在同一个 ``bash`` 上是同一件事。
"""

from __future__ import annotations

import json
from typing import Any, Optional, Tuple

from .box import Next

__all__ = ["REPEAT_REMIND_AT", "RepeatReminder"]

#: 连续相同调用第几次开始提醒（之后每隔这么多次再说一次）。
REPEAT_REMIND_AT = 3


class RepeatReminder:
    """连续相同调用达到阈值时，在结果末尾追加一句提醒（永不阻塞）。

    它只在结果后面说话，不改结果本身，也不拒绝调用 —— 是否换个做法由模型
    自己判断。``reset()`` 在一轮任务开始时调用：上一轮末尾的计数不该带到
    下一轮第一次读屏上。
    """

    def __init__(self, remind_at: int = REPEAT_REMIND_AT) -> None:
        self._remind_at = max(2, int(remind_at))
        self._last: Optional[Tuple[str, str]] = None
        self._count = 0

    def reset(self) -> None:
        self._last = None
        self._count = 0

    async def __call__(self, name: str, arguments: dict[str, Any], call: Next) -> str:
        result = await call()
        try:
            encoded = json.dumps(
                arguments, sort_keys=True, ensure_ascii=False, default=str
            )
        except (TypeError, ValueError):
            encoded = repr(arguments)
        key = (name, encoded)
        if key == self._last:
            self._count += 1
        else:
            self._last = key
            self._count = 1
        if self._count >= self._remind_at and self._count % self._remind_at == 0:
            return result + (
                f"\n\n（提醒）你已经连续 {self._count} 次完全相同地调用 {name}；"
                "先分析上次结果，若没有进展请换一种做法或结束本轮。"
            )
        return result
