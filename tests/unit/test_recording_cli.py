"""``lls recording`` 的二级命令与顶层分派。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lls import cli
from lls.config import configure
from lls.recording.cli import main as recording_main
from lls.recording.writer import TerminalRecorder


@pytest.fixture(autouse=True)
def _reset_config():
    yield
    configure()


def _record(root: Path, recording_id: str = "r1", *, rows: int = 24, columns: int = 80) -> Path:
    recorder = TerminalRecorder(
        root,
        recording_id=recording_id,
        rows=rows,
        columns=columns,
        compress=False,
        fsync=False,
    )
    recorder.record("i", b"ls\n")
    recorder.record("o", b"hello\n")
    recorder.close()
    return root / recording_id


def test_ls_lists_recordings_and_json(tmp_path, capsys) -> None:
    _record(tmp_path)

    assert recording_main(["ls", "--root", str(tmp_path)]) == 0
    output = capsys.readouterr().out
    assert "r1" in output
    assert "80x24" in output

    assert recording_main(["ls", "--root", str(tmp_path), "--json"]) == 0
    entries = json.loads(capsys.readouterr().out)
    assert entries[0]["id"] == "r1"
    assert entries[0]["state"] == "正常"
    assert entries[0]["columns"] == 80
    assert entries[0]["rows"] == 24


def test_play_dump_prints_the_final_screen(tmp_path, capsys) -> None:
    _record(tmp_path)

    assert recording_main(["play", "r1", "--root", str(tmp_path), "--dump"]) == 0
    assert "hello" in capsys.readouterr().out


def test_play_reports_a_missing_recording(tmp_path, capsys) -> None:
    assert recording_main(["play", "nope", "--root", str(tmp_path)]) == 1
    assert "找不到录制" in capsys.readouterr().err


def test_play_reports_a_deleted_recording(tmp_path, capsys) -> None:
    first = TerminalRecorder(
        tmp_path,
        recording_id="old",
        rows=24,
        columns=80,
        compress=False,
        fsync=False,
        max_recording_bytes=10_000,
        max_total_bytes=1,
    )
    first.record("o", b"x")
    first.close()
    second = TerminalRecorder(
        tmp_path,
        recording_id="new",
        rows=24,
        columns=80,
        compress=False,
        fsync=False,
        max_recording_bytes=10_000,
        max_total_bytes=1,
    )
    second.record("o", b"x")
    second.close()

    assert recording_main(["play", "old", "--root", str(tmp_path)]) == 1
    assert "已被删除" in capsys.readouterr().err


def test_export_writes_asciicast_v2(tmp_path) -> None:
    _record(tmp_path)
    output = tmp_path / "out.cast"

    assert (
        recording_main(
            ["export", "r1", "--root", str(tmp_path), "-o", str(output)]
        )
        == 0
    )
    lines = output.read_text(encoding="utf-8").splitlines()
    header = json.loads(lines[0])
    assert header["version"] == 2
    assert header["width"] == 80
    assert header["height"] == 24
    assert any(json.loads(line)[1] == "o" for line in lines[1:])


def test_top_level_dispatches_recording_ls(tmp_path, capsys) -> None:
    _record(tmp_path)

    assert cli.main(["recording", "ls", "--root", str(tmp_path)]) == 0
    assert "r1" in capsys.readouterr().out


def test_top_level_play_dump_uses_the_lls_screen(tmp_path, capsys) -> None:
    _record(tmp_path)

    assert (
        cli.main(["recording", "play", "r1", "--root", str(tmp_path), "--dump"]) == 0
    )
    assert "hello" in capsys.readouterr().out


def test_global_config_before_subcommand(tmp_path, capsys) -> None:
    state = tmp_path / "state"
    config = tmp_path / "config.yaml"
    config.write_text(f"paths:\n  root: {state.as_posix()}\n", encoding="utf-8")
    _record(state / "recordings")

    assert cli.main(["--config", str(config), "recording", "ls"]) == 0
    assert "r1" in capsys.readouterr().out


def test_double_dash_runs_a_command_named_like_a_subcommand(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(cli.subprocess, "call", lambda argv: calls.append(argv) or 0)

    assert cli.main(["--", "recording", "--help"]) == 0
    assert calls == [["recording", "--help"]]


def test_global_options_stop_at_the_first_command() -> None:
    assert cli._split_global(["--config", "x", "recording", "ls"]) == (
        ["--config", "x"],
        ["recording", "ls"],
        False,
    )
    assert cli._split_global(["bash", "--config", "x"]) == (
        [],
        ["bash", "--config", "x"],
        False,
    )
    assert cli._split_global(["--set", "a=b", "recording", "ls"]) == (
        ["--set", "a=b"],
        ["recording", "ls"],
        False,
    )
    assert cli._split_global(["--", "recording", "ls"]) == (
        [],
        ["recording", "ls"],
        True,
    )


def test_recording_without_a_subcommand_is_a_usage_error() -> None:
    with pytest.raises(SystemExit) as error:
        recording_main([])
    assert error.value.code == 2
