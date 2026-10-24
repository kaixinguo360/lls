"""内置 Role 定义。"""

from __future__ import annotations

from .role import Role

__all__ = ["DEFAULT_ROLES", "get_role"]


DEFAULT_SYSTEM = (
    "你是 shell 助手。根据用户的指令和当前的 shell 控制台输出，生成一条满足用户指令的 "
    "shell 命令。只输出命令本身，不要任何解释、不要用引号包裹、不要 markdown 代码块。"
)

DEFAULT_USER_TEMPLATE = (
    "生成一条满足用户指令的 shell 命令。\n\n"
    "用户指令：{instruct}\n\n"
    "当前控制台输出：\n{context}\n"
)

DEFAULT_ROLES: dict[str, Role] = {
    "chat": Role(
        id="chat",
        system_prompt=DEFAULT_SYSTEM,
        user_template=DEFAULT_USER_TEMPLATE,
        parsers=("strip_think", "strip_fences", "first_line"),
    ),
    "completion": Role(
        id="completion",
        system_prompt="",
        # 单段大 prompt 范式：system 与 user 合并进一个字符串。
        user_template=(
            "你是 shell 助手。根据用户指令和当前 shell 控制台输出，生成一条满足用户指令的 "
            "shell 命令。只输出命令本身。\n\n"
            "用户指令：{instruct}\n\n"
            "当前控制台输出：\n{context}\n\n"
            "命令："
        ),
        parsers=("strip_fences", "first_line"),
    ),
    "structured": Role(
        id="structured",
        system_prompt=(
            "你是 shell 助手。只输出 JSON，格式 {\"command\": \"...\", \"reason\": \"...\"}。"
        ),
        user_template=(
            "用户指令：{instruct}\n\n当前控制台输出：\n{context}\n"
        ),
        parsers=("json",),
    ),
}


def get_role(role_id: str, roles: dict[str, Role] | None = None) -> Role:
    table = roles or DEFAULT_ROLES
    try:
        return table[role_id]
    except KeyError:
        raise KeyError(f"no such role '{role_id}' (available: {', '.join(table)})") from None
