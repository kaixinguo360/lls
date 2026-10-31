"""命令目录：一份元数据同时喂补全、语法提示与帮助。

redis-cli 的补全与 hints 共用同一张命令表；这里把这件事显式化：
:class:`CommandMeta` 是唯一形状，:func:`completer_for` 把它变成输入行补全器。

只做静态匹配：候选来自命令表，不查会话、不读文件、不发请求 —— 按 Tab 永远
不会卡住，也不会有副作用。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Tuple

from lls.edit.complete import Completion, Completer, token_span

__all__ = [
    "CommandMeta",
    "completer_for",
    "registry_completer",
    "registry_metas",
]


@dataclass(frozen=True)
class CommandMeta:
    """一条命令的补全/提示元数据。

    ``usage`` 是行尾提示（含命令名，例如 ``/title [文字]``）；``subcommands``
    是参数位允许补全的静态关键词（如 ``--at``），没有就不猜。
    """

    name: str
    aliases: Tuple[str, ...] = ()
    usage: str = ""
    summary: str = ""
    subcommands: Tuple[str, ...] = ()


def _first_word(text: str) -> str:
    parts = text.lstrip().split(None, 1)
    return parts[0] if parts else ""


def _replace(text: str, start: int, stop: int, value: str) -> str:
    return text[:start] + value + text[stop:]


def _tail(usage: str, typed: str) -> str:
    """``usage`` 去掉与 ``typed`` 相同的开头：行尾只显示还没输入的那一截。

    比较大小写不敏感；``typed`` 是命令位整个词，或参数位的命令词加空格。
    """
    limit = min(len(usage), len(typed))
    index = 0
    while index < limit and usage[index].casefold() == typed[index].casefold():
        index += 1
    return usage[index:]


def completer_for(
    metas: Iterable[CommandMeta],
    *,
    marker: str = "",
    path_escape: bool = False,
) -> Completer:
    """按命令表造一个补全器。

    ``marker`` 是命令词的前缀（agent 的 ``/``；全局命令为空）。``path_escape``
    为真时，词里出现路径分隔符或以 ``.`` / ``~`` 开头就当作路径
    （``/usr/bin``、``//原样发给模型``），一个候选都不给 —— 与 agent 的
    ``_parse`` 是同一判据，补全不会把路径改成命令。
    """
    metas = tuple(metas)
    by_name = {}
    for meta in metas:
        for name in (meta.name,) + meta.aliases:
            by_name.setdefault(name.casefold(), meta)

    def complete(text: str, cursor: int) -> Completion:
        # 只在行尾补全：候选是整行，光标在中间时“后面那段怎么拼”没有唯一答案。
        if cursor != len(text):
            return Completion()
        start, stop = token_span(text, cursor)
        token = text[start:stop]
        head = text[:start]

        if head.strip():
            # 参数位：只补命令自己声明的静态子命令。行首那一段必须是本补全器
            # 认可的命令词，否则（任务文本里的普通单词）不碰。
            word = _first_word(text)
            if marker and not word.startswith(marker):
                return Completion()
            if marker:
                word = word[len(marker):]
            meta = by_name.get(word.casefold())
            if meta is None:
                return Completion()
            typed = (marker + word + " ") if marker else (word + " ")
            hint = _tail(meta.usage, typed)
            if not meta.subcommands:
                return Completion(hint=hint)
            matched = [
                sub
                for sub in meta.subcommands
                if sub.casefold().startswith(token.casefold())
            ]
            if not matched:
                return Completion(hint=hint)
            return Completion(
                tuple(_replace(text, start, stop, sub) for sub in matched), hint
            )

        # 命令位：空词不猜（空行按 Tab 只响铃，不把整张表铺上屏幕）。
        if not token:
            return Completion()
        if marker and not token.startswith(marker):
            return Completion()
        stem = token[len(marker):] if marker else token
        if path_escape and ("/" in stem or stem.startswith((".", "~"))):
            return Completion()

        found = {}
        for meta in metas:
            names = (meta.name,) + meta.aliases
            if not any(name.casefold().startswith(stem.casefold()) for name in names):
                continue
            # 别名只参与匹配，补出来的永远是规范名：/ls 按 Tab 得到 /sessions，
            # 而不是停在 /ls 上再按一次。
            found[meta.name.casefold()] = marker + meta.name
        ordered = [found[key] for key in sorted(found)]
        if not ordered:
            return Completion()

        items: List[str] = []
        for candidate in ordered:
            # 行尾补全带一个空格，接着就能打参数（redis 的命令名也这样）。
            value = candidate + " " if stop == len(text) else candidate
            items.append(_replace(text, start, stop, value))

        # 提示只在命中同一条命令时给：多条命令并列时任何一条都可能是用户想要
        # 的，先不猜；给的时候去掉已经输入的那一截（`ag` 只提示 `ent [任务]`）。
        hit = None
        ambiguous = False
        for meta in metas:
            names = (meta.name,) + meta.aliases
            if not any(name.casefold().startswith(stem.casefold()) for name in names):
                continue
            if hit is not None and hit is not meta:
                ambiguous = True
                break
            hit = meta
        if hit is None or ambiguous:
            return Completion(tuple(items))
        return Completion(tuple(items), _tail(hit.usage, token))

    return complete


def registry_metas() -> Tuple[CommandMeta, ...]:
    """注册表里每条命令一条元数据；别名不重复成条目。"""
    from lls.commands.spec import all_commands

    seen = set()
    metas = []
    for spec in all_commands().values():
        if id(spec) in seen:
            continue
        seen.add(id(spec))
        metas.append(
            CommandMeta(
                name=spec.name,
                aliases=tuple(spec.aliases),
                usage=spec.usage,
                summary=spec.summary,
                subcommands=tuple(spec.subcommands),
            )
        )
    return tuple(metas)


def registry_completer() -> Completer:
    """全局命令（``:`` 命令行）的补全器：命令词就是行首第一个词。"""
    return completer_for(registry_metas())