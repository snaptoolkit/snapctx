"""Tests for the per-mode project-preload cache stored in ``index.db``.

The TUI uses these primitives to cache an expensive "project map" blob
that's keyed by an opaque ``mode`` string ("fast" / "balanced" /
"token-saving" — but the cache treats it as a bare key). Hits must
survive process restart; any successful write primitive must
auto-invalidate the cache so we never serve a stale map.
"""

from __future__ import annotations

import concurrent.futures
from pathlib import Path

from snapctx.api import (
    add_import,
    current_source_version,
    edit_symbol_search_replace,
    get_preload,
    get_source,
    index_root,
    invalidate_preloads,
    set_preload,
)


def _build_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "__init__.py").write_text("")
    (repo / "pkg" / "math.py").write_text(
        '"""Math helpers."""\n'
        "\n"
        "\n"
        "def add(a, b):\n"
        "    return a + b\n"
        "\n"
        "\n"
        "def mul(a, b):\n"
        "    return a * b\n"
    )
    index_root(repo)
    return repo


def test_get_preload_cold_miss_returns_none(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    assert get_preload(repo, "balanced") is None


def test_set_then_get_round_trip(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    set_preload(repo, "balanced", "PROJECT MAP V1")
    assert get_preload(repo, "balanced") == "PROJECT MAP V1"


def test_set_overwrites_existing_row(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    set_preload(repo, "fast", "v1")
    set_preload(repo, "fast", "v2")
    assert get_preload(repo, "fast") == "v2"


def test_modes_are_independent(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    set_preload(repo, "fast", "FAST BLOB")
    set_preload(repo, "balanced", "BAL BLOB")
    assert get_preload(repo, "fast") == "FAST BLOB"
    assert get_preload(repo, "balanced") == "BAL BLOB"
    assert get_preload(repo, "token-saving") is None


def test_preload_survives_reopen(tmp_path: Path) -> None:
    """Cached preload must survive across process boundaries — the whole
    point of the on-disk cache is that it outlives the TUI process."""
    repo = _build_repo(tmp_path)
    set_preload(repo, "balanced", "DURABLE")
    # Different invocation, different connection — same on-disk db.
    assert get_preload(repo, "balanced") == "DURABLE"


def test_current_source_version_stable_across_calls(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    a = current_source_version(repo)
    b = current_source_version(repo)
    assert a == b
    assert isinstance(a, str) and len(a) == 40  # sha-1 hex


def test_current_source_version_changes_after_edit(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    before = current_source_version(repo)
    result = edit_symbol_search_replace(
        "pkg.math:add", "a + b", "(a + b) + 0", root=repo,
    )
    assert "error" not in result, result
    after = current_source_version(repo)
    assert before != after


def test_invalidate_preloads_drops_every_mode(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    set_preload(repo, "fast", "F")
    set_preload(repo, "balanced", "B")
    set_preload(repo, "token-saving", "T")
    invalidate_preloads(repo)
    assert get_preload(repo, "fast") is None
    assert get_preload(repo, "balanced") is None
    assert get_preload(repo, "token-saving") is None


def test_edit_symbol_search_replace_invalidates_preload(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    set_preload(repo, "balanced", "STALE MAP")
    result = edit_symbol_search_replace(
        "pkg.math:add", "a + b", "(a + b) + 0", root=repo,
    )
    assert "error" not in result, result
    assert get_preload(repo, "balanced") is None


def test_add_import_invalidates_preload(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    set_preload(repo, "fast", "STALE MAP")
    result = add_import("pkg/math.py", "import sys", root=repo)
    assert "error" not in result, result
    assert get_preload(repo, "fast") is None


def test_read_op_does_not_invalidate_preload(tmp_path: Path) -> None:
    """A pure read (``get_source``) must NOT touch the preload cache —
    otherwise every per-turn snapctx prefetch would defeat its own
    cache."""
    repo = _build_repo(tmp_path)
    set_preload(repo, "balanced", "FRESH MAP")
    src = get_source("pkg.math:add", root=repo)
    assert "error" not in src, src
    assert get_preload(repo, "balanced") == "FRESH MAP"


def test_write_primitive_physically_deletes_preload_row(tmp_path: Path) -> None:
    """Version-drift would already make ``get_preload`` return None on a
    stale row — but the spec requires the write primitives to call
    ``invalidate_preloads`` explicitly so the row is physically dropped
    (no stale-row buildup, and a future ``set_preload`` can't accidentally
    leave the old version in place). Probe the underlying table directly."""
    import sqlite3

    repo = _build_repo(tmp_path)
    set_preload(repo, "balanced", "STALE")
    db = repo / ".snapctx" / "index.db"
    assert sqlite3.connect(db).execute(
        "SELECT COUNT(*) FROM preloads"
    ).fetchone()[0] == 1

    result = edit_symbol_search_replace(
        "pkg.math:add", "a + b", "(a + b) + 0", root=repo,
    )
    assert "error" not in result, result
    assert sqlite3.connect(db).execute(
        "SELECT COUNT(*) FROM preloads"
    ).fetchone()[0] == 0


def test_concurrent_set_preload_does_not_corrupt_table(tmp_path: Path) -> None:
    """Same WAL contention shape as the imports regression
    (``test_parallel_writes_do_not_raise_database_locked``): many writers
    on one db, no ``OperationalError: database is locked``, every row
    visible after the burst, last-writer-wins semantics."""
    repo = _build_repo(tmp_path)

    def write(i: int) -> None:
        set_preload(repo, f"mode_{i % 4}", f"blob_{i}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(write, range(32)))

    # Every mode ends with SOME value (last writer wins; we don't care
    # which thread won, only that no row was lost / corrupted).
    for m in range(4):
        v = get_preload(repo, f"mode_{m}")
        assert v is not None
        assert v.startswith("blob_")
