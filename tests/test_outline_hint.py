"""``outline`` hint disambiguation when the file produces no symbols.

Regression for #12: outline previously emitted the same "Did you run
``snapctx index``?" hint for two very different cases — file unknown
to the index vs. file indexed but parser produced no symbols (a TS
schema-only or config-only module is a common example). The hint now
distinguishes the two so users don't troubleshoot the wrong thing.
"""

from __future__ import annotations

from pathlib import Path

from snapctx.api import index_root, outline


def test_outline_distinguishes_indexed_no_symbols_from_unindexed(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    # Indexable, produces a symbol.
    (repo / "pkg" / "real.py").write_text("def f():\n    return 1\n")
    # Indexable but yields no top-level symbols. Import-only is the
    # simplest reliable shape: the parser records the import (so the
    # file row lands in the ``files`` table) but emits no Symbol rows.
    (repo / "pkg" / "barren.py").write_text("import os\n")
    index_root(repo)

    barren = outline("pkg/barren.py", root=repo)
    assert barren["symbols"] == []
    assert "indexed" in barren["hint"].lower()
    assert "no symbols" in barren["hint"].lower()
    # Critically, must NOT direct the user to re-run `snapctx index`.
    assert "did you run" not in barren["hint"].lower()


def test_outline_unindexed_file_still_suggests_indexing(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "indexed.py").write_text("def f():\n    return 1\n")
    index_root(repo)

    # Created AFTER the index ran, so the file row isn't in `files`.
    (repo / "pkg" / "fresh.py").write_text("def g():\n    return 2\n")

    out = outline("pkg/fresh.py", root=repo)
    assert out["symbols"] == []
    assert "snapctx index" in out["hint"].lower()


def test_outline_missing_file_says_does_not_exist(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "real.py").write_text("def f():\n    return 1\n")
    index_root(repo)

    out = outline("pkg/no_such_file.py", root=repo)
    assert out["symbols"] == []
    assert "does not exist" in out["hint"].lower()
