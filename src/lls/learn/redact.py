"""把可能出现在日志/教师上下文里的凭据替换掉。

只做正则替换，不尝试理解内容；规则来自配置。教师看到的轨迹先脱敏，
离线读取时也复用同一组规则。
"""

from __future__ import annotations

import re
from typing import Iterable, List

__all__ = ["redact"]

_DEFAULT = (
    r"(?i)sk-[a-z0-9]{10,}",
    r"(?i)ghp_[a-z0-9]{20,}",
    r"(?i)AKIA[0-9A-Z]{16}",
)


def redact(text: str, patterns: Iterable[str] | None = None) -> str:
    rules: List[str] = list(patterns or _DEFAULT)
    result = text
    for rule in rules:
        try:
            result = re.sub(rule, "[REDACTED]", result)
        except re.error:
            continue
    return result
