"""Role —— 一种交互范式（对应 aichat 的 Role：prompt + model）。

Role 决定「怎么问」：提示词模板、上下文取多少、输出怎么解析、可选 model 覆盖。
这是你「区别于 OpenAI 对话式」初衷的最小实现：想换一种交互，就是换一个 Role。

内置三个：chat（messages 数组）、completion（单段大 prompt）、structured（JSON）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .context import Context
from .parser import apply_parsers
from .types import Message

__all__ = ["Role"]


@dataclass(frozen=True, slots=True)
class Role:
    id: str
    system_prompt: str
    user_template: str
    model: str | None = None            # None 用全局默认
    parsers: tuple[str, ...] = ("strip_think", "strip_fences", "first_line")
    context_lines: int = 30             # 截断是 Role 的策略，不是 Context 的事
    include_cwd: bool = True
    include_last_command: bool = True

    def __post_init__(self) -> None:
        # 构造期校验占位符：根治「模板缺占位符」那类静默失效。
        missing = [p for p in ("{instruct}", "{context}") if p not in self.user_template]
        if missing:
            raise ValueError(
                f"role '{self.id}' 的 user_template 缺少占位符: {', '.join(missing)}"
            )

    def build_system(self) -> Message:
        return Message(role="system", content=self.system_prompt)

    def build_user(self, instruct: str, ctx: Context) -> Message:
        context_text = self._context_text(ctx)
        content = self.user_template.format(instruct=instruct, context=context_text)
        return Message(role="user", content=content)

    def parse(self, text: str) -> str:
        return apply_parsers(text, list(self.parsers))

    def _context_text(self, ctx: Context) -> str:
        # 取「回滚区尾部 + 可见区」并截断到 context_lines。
        combined = (ctx.scrollback + "\n" + ctx.text).strip("\n")
        lines = combined.split("\n")
        if len(lines) > self.context_lines:
            lines = lines[-self.context_lines :]
        text = "\n".join(lines)
        extra: list[str] = []
        if self.include_cwd and ctx.cwd:
            extra.append(f"当前目录: {ctx.cwd}")
        if self.include_last_command and ctx.last_command:
            extra.append(f"上一条命令: {ctx.last_command}")
        if extra:
            text = "\n".join(extra) + "\n\n" + text
        return text
