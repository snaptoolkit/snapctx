"""Incremental-indexing behavior: SHA skip, targeted re-parse, and deletion cleanup."""

from __future__ import annotations

from pathlib import Path

from snapctx.api import index_root, outline


def _write_repo(root: Path) -> None:
    root.mkdir()
    (root / "a.py").write_text("def alpha(): return 1\n")
    (root / "b.py").write_text("def beta(): return 2\n")
    (root / "c.py").write_text("def gamma(): return 3\n")


def test_no_changes_skips_all_files(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _write_repo(root)
    first = index_root(root)
    assert first["files_updated"] == 3
    assert first["files_unchanged"] == 0

    # Second run — same content, everything should skip.
    second = index_root(root)
    assert second["files_updated"] == 0
    assert second["files_unchanged"] == 3
    assert second["files_removed"] == 0
    assert second["symbols_embedded"] == 0


def test_edit_reparses_only_changed_file(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _write_repo(root)
    index_root(root)

    # Change one file's content.
    (root / "b.py").write_text("def beta(): return 22\ndef delta(): return 4\n")
    second = index_root(root)
    assert second["files_updated"] == 1
    assert second["files_unchanged"] == 2

    # b.py should now contain both beta and delta.
    b_syms = {s["qname"] for s in outline("b.py", root=root)["symbols"]}
    assert "b:beta" in b_syms and "b:delta" in b_syms


def test_delete_and_add_in_same_pass_does_not_raise(tmp_path: Path) -> None:
    """Regression: forgetting a stale file then ingesting a new one in the
    same ``index_root`` pass used to raise ``OperationalError: cannot start
    a transaction within a transaction`` because ``forget_file`` left an
    auto-begun txn open and ``ingest`` then issued an explicit ``BEGIN``.
    """
    root = tmp_path / "repo"
    _write_repo(root)
    index_root(root)

    # Delete one file (forces forget_file) AND add one (forces ingest)
    # in the same incremental pass.
    (root / "c.py").unlink()
    (root / "d.py").write_text("def delta(): return 4\n")
    summary = index_root(root)
    assert summary["files_removed"] == 1
    assert summary["files_updated"] == 1


def test_root_rename_wipes_and_rebuilds(tmp_path: Path) -> None:
    """Regression: moving the project on disk left every absolute path in
    the index pointing at the old location. ``index_root`` now detects the
    mismatch and wipes the index so the rebuild repopulates with current
    paths."""
    import shutil

    original = tmp_path / "old-name"
    _write_repo(original)
    index_root(original)

    # Simulate ``mv old-name new-name`` (incl. the ``.snapctx/`` dir).
    moved = tmp_path / "new-name"
    shutil.move(str(original), str(moved))

    summary = index_root(moved)
    assert summary["root_moved"] is True
    # Wipe-then-rebuild: every file is "updated" (re-parsed), nothing is
    # "removed" (the wipe happened before the staleness diff).
    assert summary["files_updated"] == 3
    assert summary["files_removed"] == 0

    # Queries against the new location resolve to real files.
    syms = outline("a.py", root=moved)["symbols"]
    assert any(s["qname"] == "a:alpha" for s in syms)


def test_deleted_file_is_removed_from_index(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _write_repo(root)
    index_root(root)

    # Verify c.py's symbols are present initially.
    c_before = outline("c.py", root=root)
    assert any(s["qname"] == "c:gamma" for s in c_before["symbols"])

    # Delete c.py and re-index.
    (root / "c.py").unlink()
    second = index_root(root)
    assert second["files_removed"] == 1

    # Outline for c.py should now be empty (no symbols indexed for it).
    c_after = outline("c.py", root=root)
    assert c_after["symbols"] == []
