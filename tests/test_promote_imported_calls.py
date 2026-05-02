"""Tests for the third call-resolution pass: ``promote_imported_calls``.

Background: the parser optimistically resolves intra-file calls but
leaves cross-module bare-name calls with ``callee_qname=NULL`` because
following the import graph at parse time is fragile. Two patterns end
up unresolved without help:

1. Source-tree prefix mismatch — the file does
   ``from pkg.mod import foo`` but the symbol's qname is
   ``src.pkg.mod:foo`` (because the source tree is rooted at ``src/``).

2. Re-exports — the file does ``from pkg import foo`` and the actual
   definition lives in ``src.pkg._inner:foo``, surfaced through
   ``pkg/__init__.py``.

Both leave ``snapctx expand --direction callers`` blind to the
caller; the fix is a post-ingest pass that bridges the gap by
matching the imports table against the symbols table.

The pass MUST stay conservative: a bare name with multiple compatible
candidates is ambiguous, and resolving it incorrectly would corrupt
``snapctx expand`` output. The contract: resolve when uniquely
inferable, skip otherwise.
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
        rows = idx.callers_of(callee_qname)
    finally:
        idx.close()
    return {r["caller_qname"] for r in rows}


def test_resolves_direct_module_import_with_src_prefix(tmp_path: Path) -> None:
    """``from pkg.mod import foo`` with the symbol at ``src.pkg.mod:foo``."""
    repo = tmp_path / "repo"
    (repo / "src" / "pkg").mkdir(parents=True)
    (repo / "src" / "pkg" / "__init__.py").write_text("")
    (repo / "src" / "pkg" / "mod.py").write_text(
        "def foo():\n    return 1\n"
    )
    (repo / "tests").mkdir()
    (repo / "tests" / "test_caller.py").write_text(
        "from pkg.mod import foo\n"
        "\n"
        "def caller():\n"
        "    foo()\n"
    )
    index_root(repo)
    callers = _callers_of(repo, "src.pkg.mod:foo")
    assert "tests.test_caller:caller" in callers


def test_resolves_reexport_from_package_init(tmp_path: Path) -> None:
    """``from pkg import foo`` where the def is in ``src.pkg._inner:foo``,
    surfaced via ``pkg/__init__.py: from pkg._inner import foo``."""
    repo = tmp_path / "repo"
    (repo / "src" / "pkg").mkdir(parents=True)
    (repo / "src" / "pkg" / "__init__.py").write_text(
        "from pkg._inner import foo\n"
        "\n"
        "__all__ = ['foo']\n"
    )
    (repo / "src" / "pkg" / "_inner.py").write_text(
        "def foo():\n    return 1\n"
    )
    (repo / "tests").mkdir()
    (repo / "tests" / "test_caller.py").write_text(
        "from pkg import foo\n"
        "\n"
        "def caller():\n"
        "    foo()\n"
    )
    index_root(repo)
    callers = _callers_of(repo, "src.pkg._inner:foo")
    assert "tests.test_caller:caller" in callers


def test_skips_when_caller_did_not_import(tmp_path: Path) -> None:
    """Bare-name call without an import — could be a builtin or a
    local global. We must not invent a binding."""
    repo = tmp_path / "repo"
    (repo / "src" / "pkg").mkdir(parents=True)
    (repo / "src" / "pkg" / "__init__.py").write_text("")
    (repo / "src" / "pkg" / "mod.py").write_text(
        "def foo():\n    return 1\n"
    )
    (repo / "src" / "pkg" / "caller.py").write_text(
        # No import of foo — this `foo()` could be a typo or a global.
        "def caller():\n"
        "    foo()\n"
    )
    index_root(repo)
    callers = _callers_of(repo, "src.pkg.mod:foo")
    assert "src.pkg.caller:caller" not in callers


def test_skips_when_ambiguous(tmp_path: Path) -> None:
    """Two symbols share the bare name and both are import-compatible —
    we have no signal to pick one. Must not resolve."""
    repo = tmp_path / "repo"
    (repo / "src" / "pkg").mkdir(parents=True)
    (repo / "src" / "pkg" / "__init__.py").write_text(
        "from pkg.mod_a import foo\n"  # makes pkg.mod_a:foo compatible too
        "from pkg.mod_b import foo as foo_b\n"
    )
    (repo / "src" / "pkg" / "mod_a.py").write_text(
        "def foo():\n    return 1\n"
    )
    (repo / "src" / "pkg" / "mod_b.py").write_text(
        "def foo():\n    return 2\n"
    )
    (repo / "tests").mkdir()
    (repo / "tests" / "test_caller.py").write_text(
        "from pkg import foo\n"  # pkg's __init__ has foo from mod_a AND
        # pkg.mod_b also has a `foo` — both qnames have last-segment 'foo'
        # AND start with 'src.pkg.', so target='pkg' makes both compatible.
        "\n"
        "def caller():\n"
        "    foo()\n"
    )
    index_root(repo)
    callers_a = _callers_of(repo, "src.pkg.mod_a:foo")
    callers_b = _callers_of(repo, "src.pkg.mod_b:foo")
    # We refuse to guess — neither side claims the call.
    assert "tests.test_caller:caller" not in callers_a
    assert "tests.test_caller:caller" not in callers_b


def test_resolves_via_alias(tmp_path: Path) -> None:
    """``from pkg.mod import foo as f`` — the call site uses ``f()``.
    The imports table records alias='f', name='foo'. The pass should
    match against alias and resolve to the underlying qname."""
    repo = tmp_path / "repo"
    (repo / "src" / "pkg").mkdir(parents=True)
    (repo / "src" / "pkg" / "__init__.py").write_text("")
    (repo / "src" / "pkg" / "mod.py").write_text(
        "def foo():\n    return 1\n"
    )
    (repo / "tests").mkdir()
    (repo / "tests" / "test_caller.py").write_text(
        "from pkg.mod import foo as f\n"
        "\n"
        "def caller():\n"
        "    f()\n"
    )
    index_root(repo)
    callers = _callers_of(repo, "src.pkg.mod:foo")
    assert "tests.test_caller:caller" in callers
