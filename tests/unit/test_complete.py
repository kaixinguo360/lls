"""补全纯逻辑：候选、标记、路径豁免与语法提示。"""

from __future__ import annotations

from lls.commands.catalog import CommandMeta, completer_for
from lls.edit.complete import clip_hint, display_width, token_span

SLASH = (
    CommandMeta("help", ("h", "?"), "/help"),
    CommandMeta("status", ("s",), "/status"),
    CommandMeta("title", (), "/title [文字]"),
    CommandMeta("fork", (), "/fork [--at <seq>]", subcommands=("--at", "--force")),
)

COMPLETE = completer_for(SLASH, marker="/", path_escape=True)


def test_prefix_lists_every_command_in_name_order() -> None:
    assert COMPLETE("/", 1).items == ("/fork ", "/help ", "/status ", "/title ")


def test_a_single_match_completes_the_line_with_a_trailing_space() -> None:
    assert COMPLETE("/st", 3).items == ("/status ",)
    assert COMPLETE("/t", 2).items == ("/title ",)


def test_matching_is_case_insensitive_and_prints_the_canonical_name() -> None:
    assert COMPLETE("/ST", 3).items == ("/status ",)


def test_an_alias_completes_to_the_command_itself() -> None:
    assert COMPLETE("/h", 2).items == ("/help ",)
    assert COMPLETE("/?", 2).items == ("/help ",)


def test_an_unknown_prefix_has_no_candidates() -> None:
    assert COMPLETE("/zz", 3).items == ()


def test_an_empty_line_does_not_dump_the_whole_table() -> None:
    assert COMPLETE("", 0).items == ()


def test_paths_are_left_alone_even_when_they_start_with_a_slash() -> None:
    assert COMPLETE("/usr/bin", 8).items == ()
    assert COMPLETE("//原样交给模型", len("//原样交给模型")).items == ()


def test_only_the_end_of_the_line_is_completed() -> None:
    assert COMPLETE("/st", 2).items == ()


def test_argument_completion_uses_declared_subcommands_and_keeps_the_command() -> None:
    assert COMPLETE("/fork --a", len("/fork --a")).items == ("/fork --at",)
    assert COMPLETE("/fork ", len("/fork ")).items == (
        "/fork --at",
        "/fork --force",
    )


def test_a_command_without_subcommands_completes_nothing_but_still_hints() -> None:
    result = COMPLETE("/title x", len("/title x"))
    assert result.items == ()
    assert result.hint == "/title [文字]"


def test_hint_only_for_a_complete_command() -> None:
    assert COMPLETE("/title", 6).hint == "/title [文字]"
    assert COMPLETE("/ti", 3).hint == ""
    assert COMPLETE("/title ", 7).hint == "/title [文字]"


def test_without_a_marker_the_first_word_is_the_command() -> None:
    complete = completer_for(SLASH)
    assert complete("st", 2).items == ("status ",)
    assert complete("st", 1).items == ()


def test_token_span_and_display_width() -> None:
    assert token_span("/status x", 3) == (0, 7)
    assert token_span("/status", 7) == (0, 7)
    assert display_width("中文") == 4


def test_hint_is_single_line_and_cut_by_columns() -> None:
    assert clip_hint("a\nb", 5) == "a b"
    assert clip_hint("中文中文", 5) == "中文"
    assert clip_hint("标题提示", 2) == "标"
    assert clip_hint("abc", 0) == ""


def test_registry_metas_carry_usage_for_the_two_command_sets() -> None:
    import lls.commands  # noqa: F401  # 导入触发内置命令注册

    from lls.commands.catalog import registry_metas

    metas = {meta.name: meta for meta in registry_metas()}
    assert metas["agent"].usage == "agent [任务]"
    assert metas["sessions"].usage == "sessions [--all]"
    assert metas["fork"].usage == "fork <id> [--at <seq>]"

def test_the_agent_completer_reuses_the_slash_table() -> None:
    from lls.commands.builtin.agent import _SLASH_COMPLETER

    assert _SLASH_COMPLETER("/st", 3).items == ("/status ",)
    assert _SLASH_COMPLETER("/model", 6).hint == "/model [provider/model]"
    assert _SLASH_COMPLETER("/usr/bin", 8).items == ()
    assert _SLASH_COMPLETER("/nope", 5).items == ()


def test_the_global_command_set_completes_through_the_registry() -> None:
    from lls.commands.catalog import registry_completer

    complete = registry_completer()
    assert complete("ag", 2).items == ("agent ",)
    assert complete("agent ", 6).hint == "agent [任务]"