"""录制包必须能整体挪走：内部不得 import ``lls.*``，核心不得依赖 pyte。"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "lls" / "recording"


def _files() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def test_recording_package_never_imports_lls() -> None:
    offenders = []
    for path in _files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.level == 0 and (node.module or "").split(".")[0] == "lls":
                    offenders.append(f"{path.relative_to(SRC)}:{node.lineno}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] == "lls":
                        offenders.append(f"{path.relative_to(SRC)}:{node.lineno}")
    assert not offenders, "录制包内部出现了 lls.* 导入：" + ", ".join(offenders)


def test_core_recording_does_not_import_pyte() -> None:
    offenders = []
    for path in _files():
        if "replay" in path.relative_to(SRC).parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "pyte":
                offenders.append(str(path.relative_to(SRC)))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "pyte":
                        offenders.append(str(path.relative_to(SRC)))
    assert not offenders, "录制核心不应依赖 pyte：" + ", ".join(offenders)
