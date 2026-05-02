"""Per-file progress callback for ``index_root``.

UI consumers (e.g. codebeaver-tui) want to render a progress bar that
ticks once per file, not once per root. The ``progress_callback`` kwarg
makes that possible without forcing the caller to walk the index db
themselves.

Contract:

* keyword-only kwarg
* invoked once per file processed inside the main loop (updated,
  skipped, OR removed)
* receives ``(current, total, path)`` where ``current`` is 1-indexed
  and ``total`` is fixed for the whole pass
* exceptions raised by the callback do NOT abort indexing
"""

from __future__ import annotations

from pathlib import Path

from snapctx.api import index_root


def _write_repo(root: Path) -> None:
    root.mkdir()
    (root / "a.py").write_text("def alpha(): return 1\n")
    (root / "b.py").write_text("def beta(): return 2\n")
    (root / "c.py").write_text("def gamma(): return 3\n")


def test_progress_callback_invoked_once_per_file(tmp_path: Path) -> None:
    """Three source files → three callback invocations with 1-indexed
    ``current``, the same ``total`` each time, and a non-empty ``path``.
    """
    root = tmp_path / "repo"
    _write_repo(root)

    calls: list[tuple[int, int, str]] = []

    def cb(current: int, total: int, path: str) -> None:
        calls.append((current, total, path))

    summary = index_root(root, progress_callback=cb)

    # One callback per file in the pass.
    assert len(calls) == 3, f"expected 3 calls, got {len(calls)}: {calls}"

    # current is 1-indexed and monotonically increasing.
    currents = [c for c, _, _ in calls]
    assert currents == [1, 2, 3], f"expected 1-indexed monotonic, got {currents}"

    # total is consistent and equals files_scanned (no removals on first run).
    totals = {t for _, t, _ in calls}
    assert totals == {3}, f"total should be the same on every call, got {totals}"
    assert summary["files_scanned"] == 3

    # path is a non-empty string for every report.
    for _, _, p in calls:
        assert isinstance(p, str)
        assert p, "path should be non-empty"


def test_progress_callback_exception_does_not_abort_indexing(
    tmp_path: Path,
) -> None:
    """A misbehaving callback (raising on every call) must not break the
    indexer — ``index_root`` still returns its summary dict and every
    file is still ingested.
    """
    root = tmp_path / "repo"
    _write_repo(root)

    call_count = 0

    def angry_cb(current: int, total: int, path: str) -> None:
        nonlocal call_count
        call_count += 1
        raise RuntimeError("boom")

    summary = index_root(root, progress_callback=angry_cb)

    # The indexer kept calling us even after we raised.
    assert call_count == 3, f"expected 3 attempts, got {call_count}"
    # And it still produced a complete summary dict.
    assert summary["files_scanned"] == 3
    assert summary["files_updated"] == 3
    assert "symbols_indexed" in summary


def test_progress_callback_includes_removed_files(tmp_path: Path) -> None:
    """Stale (removed) files count in ``total`` and trigger their own
    callback report. Callers want a single bar that ticks across both
    ingest and cleanup phases.
    """
    root = tmp_path / "repo"
    _write_repo(root)
    index_root(root)  # initial index — populates db with a/b/c

    # Remove one file. Now: 2 walker_files + 1 stale = 3 total.
    (root / "c.py").unlink()

    calls: list[tuple[int, int, str]] = []

    def cb(current: int, total: int, path: str) -> None:
        calls.append((current, total, path))

    summary = index_root(root, progress_callback=cb)

    assert summary["files_removed"] == 1
    # 2 unchanged + 1 removed = 3 reports
    assert len(calls) == 3, f"expected 3 calls, got {len(calls)}: {calls}"
    totals = {t for _, t, _ in calls}
    assert totals == {3}, f"total should be 3 (2 walker + 1 stale), got {totals}"

    # All paths non-empty; the removed file's path appears.
    paths = {p for _, _, p in calls}
    assert all(paths)
    assert any(p.endswith("c.py") for p in paths)
