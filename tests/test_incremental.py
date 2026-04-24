"""Incremental-indexing behavior: SHA skip, targeted re-parse, and deletion cleanup."""

from __future__ import annotations

from pathlib import Path

from neargrep.api import index_root, outline


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
