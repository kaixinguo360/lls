"""守住 ``requires-python = ">=3.8"`` 这个声明。

``from __future__ import annotations`` 只把**注解**变成字符串。下列位置仍会在
导入时求值，3.8 上会直接抛 ``TypeError``：

* 裸赋值 —— ``DirtyRows = frozenset[int]``（PEP 585 要 3.9，报
  ``'type' object is not subscriptable``）、``Fn = Callable[[str | None], ...]``
  （PEP 604 要 3.10）；
* ``cast()`` / ``isinstance()`` 的参数；
* 缺少 ``from __future__ import annotations`` 时，该文件里的**所有**注解。

这些都是导入即崩，比运行时才发现更早，但没有解释器就测不出来 —— 所以在此静态
扫一遍。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"

# PEP 585：3.9 起这些内建才可下标
BUILTIN_GENERICS = {"list", "dict", "set", "frozenset", "tuple", "type"}


def _sources() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def _is_null_constant(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


def _is_pep604(node: ast.AST) -> bool:
    """``A | B`` 形式的联合类型。"""
    if not isinstance(node, ast.BinOp) or not isinstance(node.op, ast.BitOr):
        return False
    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, ast.BinOp) and isinstance(current.op, ast.BitOr):
            stack.extend([current.left, current.right])
        elif isinstance(current, ast.Name) or _is_null_constant(current):
            continue
        else:
            return False
    return True


def _is_pep585(node: ast.AST) -> bool:
    """``frozenset[int]`` 形式的内建泛型。"""
    return (
        isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Name)
        and node.value.id in BUILTIN_GENERICS
    )


def _runtime_types(tree: ast.Module):
    """产出会在导入时求值的类型表达式所在行号。"""
    for node in ast.walk(tree):
        # 裸赋值：值整体被求值
        if isinstance(node, (ast.Module, ast.ClassDef)):
            for stmt in node.body:
                if isinstance(stmt, ast.Assign):
                    for sub in ast.walk(stmt.value):
                        if _is_pep604(sub) or _is_pep585(sub):
                            yield stmt.lineno
        # 注解赋值的右值同样被求值
        if isinstance(node, ast.AnnAssign) and node.value is not None:
            for sub in ast.walk(node.value):
                if _is_pep604(sub) or _is_pep585(sub):
                    yield node.lineno
        # cast() / isinstance() 的参数
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name in ("cast", "isinstance"):
                for arg in node.args:
                    for sub in ast.walk(arg):
                        if _is_pep604(sub) or _is_pep585(sub):
                            yield node.lineno


def test_no_pep604_or_pep585_at_import_time() -> None:
    offenders = []
    for path in _sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for lineno in _runtime_types(tree):
            offenders.append(f"{path.relative_to(SRC.parent)}:{lineno}")

    assert not offenders, "导入时会对内置类型下标（3.9+）或使用 | 联合（3.10+）：" + ", ".join(
        offenders
    )


def _annotations(tree: ast.Module):
    """产出该文件里所有注解节点。"""
    for node in ast.walk(tree):
        if isinstance(node, ast.arg) and node.annotation is not None:
            yield node.annotation
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.returns:
            yield node.returns
        if isinstance(node, ast.AnnAssign):
            yield node.annotation


def _needs_postponed(tree: ast.Module) -> bool:
    """注解里是否用了 PEP 604 / 585 —— 这类文件必须靠 future import 续命。"""
    for annotation in _annotations(tree):
        for sub in ast.walk(annotation):
            if _is_pep604(sub) or _is_pep585(sub):
                return True
    return False


@pytest.mark.parametrize("path", _sources(), ids=lambda p: str(p.name))
def test_modern_annotations_require_future_import(path: Path) -> None:
    """注解被求值时，3.8 会在导入阶段就抛 TypeError。"""
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text, filename=str(path))
    if not _needs_postponed(tree):
        return
    assert "from __future__ import annotations" in text, (
        f"{path} 的注解用了 3.9+ 语法，但缺少 `from __future__ import annotations`"
    )


def test_sources_are_parsable_as_38() -> None:
    for path in _sources():
        ast.parse(
            path.read_text(encoding="utf-8"),
            filename=str(path),
            feature_version=(3, 8),
        )
