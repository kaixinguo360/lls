"""输入历史的落盘：追加、去重、裁剪、作用域，以及引擎里的接线。"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from lls.edit import HistoryStore, forget_session, history_from_config
from lls.edit.buffer import LineEditor
from lls.session.engine import Engine


def _write(path: Path, *texts: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(_line(t) for t in texts))


def _line(text: str) -> bytes:
    return (json.dumps({"v": 1, "text": text}, ensure_ascii=False) + "\n").encode("utf-8")


# ---- 基本读写 ----

def test_appends_in_order(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path)
    store.append("cmd", "ls -l")
    store.append("cmd", "git status")

    assert store.load("cmd") == ["ls -l", "git status"]


def test_consecutive_duplicates_are_dropped(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path)
    store.append("cmd", "ls")
    store.append("cmd", "ls")
    store.append("cmd", "pwd")

    assert store.load("cmd") == ["ls", "pwd"]


def test_repeat_after_restart_is_dropped(tmp_path: Path) -> None:
    """新进程第一次写要先看盘上的最后一条，否则重启后会写进两遍。"""
    HistoryStore(tmp_path).append("cmd", "make test")

    HistoryStore(tmp_path).append("cmd", "make test")

    assert HistoryStore(tmp_path).load("cmd") == ["make test"]


def test_blank_input_is_not_stored(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path)
    store.append("cmd", "   ")
    store.append("cmd", "")

    assert store.load("cmd") == []
    assert not store.path_for("cmd").exists()


def test_empty_history_reads_as_empty_list(tmp_path: Path) -> None:
    assert HistoryStore(tmp_path).load("cmd") == []


def test_ids_do_not_mix(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path)
    store.append("cmd", "ls")
    store.append("agent_task", "refactor the parser")

    assert store.load("cmd") == ["ls"]
    assert store.load("agent_task") == ["refactor the parser"]


# ---- 坏数据与容量 ----

def test_half_written_tail_is_ignored(tmp_path: Path) -> None:
    """进程被杀时尾行只写了一半：能读的照读，读不了的跳过。"""
    store = HistoryStore(tmp_path)
    path = store.path_for("cmd")
    _write(path, "ls")
    with open(str(path), "ab") as fh:
        fh.write(b'{"v": 1, "text": "git co')

    assert store.load("cmd") == ["ls"]
    store.append("cmd", "pwd")
    assert store.load("cmd") == ["ls", "pwd"]


def test_load_keeps_only_the_tail(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path, max_entries=3)
    for text in ("a", "b", "c", "d", "e"):
        store.append("cmd", text)

    assert store.load("cmd") == ["c", "d", "e"]


def test_oversized_file_is_compacted_on_load(tmp_path: Path) -> None:
    """载入时顺手压缩：文件不会只增不减，且压缩后还能继续追加。"""
    store = HistoryStore(tmp_path, max_entries=2)
    path = store.path_for("cmd")
    _write(path, *("cmd-%d" % i for i in range(50)))

    assert store.load("cmd") == ["cmd-48", "cmd-49"]
    assert len(path.read_bytes().splitlines()) == 2

    store.append("cmd", "pwd")
    assert store.load("cmd") == ["cmd-49", "pwd"]


def test_same_value_twice_apart_is_kept(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path)
    store.append("cmd", "ls")
    store.append("cmd", "pwd")
    store.append("cmd", "ls")

    assert store.load("cmd") == ["ls", "pwd", "ls"]


# ---- 作用域：哪些历史跟会话走 ----

def test_scope_is_declared_by_the_caller(tmp_path: Path) -> None:
    """跟着会话走的不是某几个 id，而是传了 ``session=True`` 的那几处。"""
    store = HistoryStore(tmp_path, scope_of=lambda: "s-42")
    store.append("cmd", "ls")
    store.append("agent_task", "rename the flag", session=True)

    assert store.path_for("cmd").parent.name == "global"
    assert store.path_for("cmd", session=True).parent.name == "session-s-42"
    assert store.load("cmd", session=True) == []


def test_session_switch_moves_ai_history(tmp_path: Path) -> None:
    current = ["s-1"]
    store = HistoryStore(tmp_path, scope_of=lambda: current[0])
    store.append("instruct", "写得短一点", session=True)
    current[0] = "s-2"

    assert store.load("instruct", session=True) == []
    assert HistoryStore(tmp_path, scope_of=lambda: "s-1").load(
        "instruct", session=True
    ) == ["写得短一点"]


def test_unknown_session_uses_global(tmp_path: Path) -> None:
    """还没进 agent、会话被删了：宁可共用，也不能把输入丢了。"""
    store = HistoryStore(tmp_path, scope_of=lambda: "")
    store.append("agent_task", "hello")

    assert store.path_for("agent_task").parent.name == "global"
    assert store.load("agent_task") == ["hello"]


def test_broken_scope_resolver_falls_back_to_global(tmp_path: Path) -> None:
    def boom() -> str:
        raise RuntimeError("没有会话")

    store = HistoryStore(tmp_path, scope_of=boom)
    store.append("agent_task", "hello")

    assert store.load("agent_task") == ["hello"]


def test_ids_with_slashes_stay_inside_root(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path)
    path = store.path_for("../../etc/passwd")

    assert tmp_path in path.parents or path.parent == tmp_path
    store.append("../../etc/passwd", "x")
    assert path.exists()


def test_forget_session_removes_only_that_session(tmp_path: Path) -> None:
    for session_id, text in (("s-1", "one"), ("s-2", "two")):
        store = HistoryStore(tmp_path, scope_of=lambda: session_id)
        store.append("agent_task", text, session=True)

    forget_session("s-1", tmp_path)

    assert not (tmp_path / "session-s-1").exists()
    assert (tmp_path / "session-s-2").exists()


# ---- 配置开关 ----

def _configure(monkeypatch, tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setenv("LLS_CONFIG", str(path))
    return path


def test_switch_off_disables_store(monkeypatch, tmp_path: Path) -> None:
    _configure(
        monkeypatch,
        tmp_path,
        "paths:\n  root: {}\ninput_history:\n  enabled: false\n".format(tmp_path),
    )

    assert history_from_config() is None


def test_enabled_by_default_and_reads_limits(monkeypatch, tmp_path: Path) -> None:
    _configure(
        monkeypatch,
        tmp_path,
        "paths:\n  root: {}\ninput_history:\n  max_entries: 7\n".format(tmp_path),
    )

    store = history_from_config()

    assert store is not None
    store.append("cmd", "ls")
    assert (tmp_path / "history" / "global" / "cmd.jsonl").exists()
    assert store._max_entries == 7


def test_bad_limit_falls_back(monkeypatch, tmp_path: Path) -> None:
    _configure(
        monkeypatch,
        tmp_path,
        "paths:\n  root: {}\ninput_history:\n  max_entries: not-a-number\n".format(tmp_path),
    )

    with pytest.warns(RuntimeWarning, match="input_history.max_entries"):
        store = history_from_config()

    assert store is not None
    assert store._max_entries == 200


# ---- 引擎接线 ----

class _Screen:
    rows = 24
    columns = 80


def _engine(store: HistoryStore | None) -> Engine:
    return Engine(
        backend=None,  # type: ignore[arg-type]
        argv=["sh"],
        screen=_Screen(),  # type: ignore[arg-type]
        handlers={},
        initial_handler="default",
        history=store,
    )


def test_engine_loads_saved_history_for_up_arrow(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path)
    store.append("cmd", "ls -l")

    editor = _engine(store)._editor_for("cmd")
    editor.history_up()

    assert editor.text == "ls -l"


def test_engine_remembers_submitted_lines(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path)
    engine = _engine(store)
    editor = engine._editor_for("cmd")
    editor.insert("git status")
    engine._remember("cmd", editor.submit())

    assert HistoryStore(tmp_path).load("cmd") == ["git status"]


def test_engine_without_store_keeps_history_in_memory_only() -> None:
    """没接 store 时一切照旧：↑ 只在本次进程里有用，落盘调用是空操作。"""
    engine = _engine(None)
    editor = engine._editor_for("cmd")
    editor.insert("ls")
    editor.submit()
    engine._remember("cmd", "ls")
    engine.record_history("plain", history_id="agent_task")

    assert editor.history == ["ls"]
    assert engine._editor_for("cmd") is editor
    assert engine._editor_for("agent_task").history == ["plain"]


def test_record_history_persists(tmp_path: Path) -> None:
    engine = _engine(HistoryStore(tmp_path))
    engine.record_history("summarise the diff", history_id="agent_task")

    assert HistoryStore(tmp_path).load("agent_task") == ["summarise the diff"]


def test_compaction_that_cannot_shrink_leaves_the_file_alone(tmp_path: Path) -> None:
    """单行很长时按字节数触发的压缩丢不掉东西，不该每次都重写一遍。"""
    store = HistoryStore(tmp_path, max_bytes=64)
    store.append("cmd", "x" * 400)
    path = store.path_for("cmd")
    before = path.stat()

    HistoryStore(tmp_path, max_bytes=64).load("cmd")

    after = path.stat()
    assert (after.st_ino, after.st_mtime_ns, after.st_size) == (
        before.st_ino,
        before.st_mtime_ns,
        before.st_size,
    )


def test_history_files_are_private(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path)
    store.append("cmd", "export TOKEN=…")
    path = store.path_for("cmd")

    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_switching_session_reloads_the_editor(tmp_path: Path) -> None:
    """中途换了会话，↑ 只该翻到当前会话的输入。"""
    current = ["s-1"]
    store = HistoryStore(tmp_path, scope_of=lambda: current[0])
    store.append("instruct", "上一个会话的指令", session=True)
    engine = _engine(store)
    editor = engine._editor_for("instruct", session=True)
    editor.history_up()
    assert editor.text == "上一个会话的指令"

    current[0] = "s-2"
    fresh = engine._editor_for("instruct", session=True)
    assert fresh is not editor
    assert fresh.history == []


# ---- 写失败：不打扰输入 ----

def test_write_failure_is_recorded_quietly(tmp_path: Path, capsys) -> None:
    """历史存不下时照常能干活：只记下原因，不往终端打字。"""
    blocked = tmp_path / "global"
    blocked.write_text("", encoding="utf-8")  # 本该是目录，占成文件
    store = HistoryStore(tmp_path)

    store.append("cmd", "ls")

    assert store.last_error is not None
    assert "global" in store.last_error
    assert capsys.readouterr().err == ""


def test_partial_writes_are_finished(tmp_path: Path, monkeypatch) -> None:
    """``os.write`` 可能只写一部分；剩下的必须补上，否则历史行会残缺。"""
    real_write = os.write

    def stingy(fd: int, data) -> int:
        return real_write(fd, bytes(data[:3]))

    monkeypatch.setattr(os, "write", stingy)
    store = HistoryStore(tmp_path)
    store.append("cmd", "git commit -m '写一段够长的提交说明'")

    monkeypatch.setattr(os, "write", real_write)
    assert HistoryStore(tmp_path).load("cmd") == ["git commit -m '写一段够长的提交说明'"]
