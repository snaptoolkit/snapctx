"""Tests for ``promote_local_calls`` — same-module bare-name call resolution.

The Python parser records a call to a sibling function defined in the
same module as a bare-name call (``callee_qname=NULL``). After all
files are ingested we know which file owns each symbol, so we can
resolve any bare name to the symbol with the same module-half + same
file. ``promote_self_calls`` already handles ``self.X`` / ``this.X``;
``promote_imported_calls`` handles cross-module references via the
imports table; this pass closes the third gap.

Surfaced by the strategy benchmark (T2): ``snapctx expand
src.snapctx.api._edit_batch:_apply_to_one_file --direction callers``
returned an empty layer because ``edit_symbol_batch``'s bare-name
call ``_apply_to_one_file(path, file_edits, root_path)`` was never
resolved. After this pass, expand returns the caller correctly.
"""

from __future__ import annotations

from pathlib import Path

from snapctx.api import index_root
from snapctx.index import Index, db_path_for


def _open(repo: Path) -> Index:
    return Index(db_path_for(repo))


def _callers_of(repo: Path, callee_qname: str) -> set[str]:
    idx = _open(repo)
    try:
        return {r["caller_qname"] for r in idx.callers_of(callee_qname)}
    finally:
        idx.close()


def test_same_module_function_call_resolves(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "__init__.py").write_text("")
    (repo / "pkg" / "mod.py").write_text(
        "def helper(x):\n"
        "    return x + 1\n"
        "\n"
        "\n"
        "def caller(x):\n"
        "    return helper(x) * 2\n"
    )
    index_root(repo)
    callers = _callers_of(repo, "pkg.mod:helper")
    assert "pkg.mod:caller" in callers


def test_does_not_match_method_with_same_simple_name(tmp_path: Path) -> None:
    """A class method ``Cls.foo`` should NOT be resolved to from a
    bare-name call ``foo()`` in the same file — that's an attribute
    access, not a free function call. We restrict matches to top-level
    callable symbols (member half has no ``.``)."""
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "__init__.py").write_text("")
    (repo / "pkg" / "mod.py").write_text(
        "class Cls:\n"
        "    def foo(self):\n"
        "        return 1\n"
        "\n"
        "\n"
        "def caller():\n"
        "    return foo()\n"  # references something else; not Cls.foo
    )
    index_root(repo)
    # The method's qname is pkg.mod:Cls.foo — caller MUST NOT show as
    # a caller of it (the bare ``foo()`` is unresolved, not a method
    # call on an instance).
    callers = _callers_of(repo, "pkg.mod:Cls.foo")
    assert "pkg.mod:caller" not in callers


def test_does_not_cross_modules(tmp_path: Path) -> None:
    """Two modules each define ``foo``; a bare-name ``foo()`` call in
    one module must NOT resolve to the other's ``foo``. File equality
    is the gate."""
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "__init__.py").write_text("")
    (repo / "pkg" / "a.py").write_text(
        "def foo():\n    return 1\n\n\n"
        "def caller_in_a():\n    return foo()\n"
    )
    (repo / "pkg" / "b.py").write_text(
        "def foo():\n    return 2\n"
    )
    index_root(repo)
    # caller_in_a should resolve to pkg.a:foo, NOT pkg.b:foo.
    callers_a = _callers_of(repo, "pkg.a:foo")
    callers_b = _callers_of(repo, "pkg.b:foo")
    assert "pkg.a:caller_in_a" in callers_a
    assert "pkg.a:caller_in_a" not in callers_b


def test_call_to_class_in_same_module_resolves(tmp_path: Path) -> None:
    """Instantiation ``MyCls()`` is a bare-name call too; resolve to
    the class qname when ``MyCls`` lives in the same module."""
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "__init__.py").write_text("")
    (repo / "pkg" / "mod.py").write_text(
        "class Box:\n"
        "    pass\n"
        "\n"
        "\n"
        "def make():\n"
        "    return Box()\n"
    )
    index_root(repo)
    callers = _callers_of(repo, "pkg.mod:Box")
    assert "pkg.mod:make" in callers
