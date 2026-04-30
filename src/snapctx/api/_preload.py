"""Per-mode "project preload" cache, persisted in the root's ``index.db``.

The TUI renders a "project map" — an expensive aggregate of skeleton +
context — that it wants to prepend to every prompt. Re-rendering it on
every keystroke is wasteful; rendering it once per *mode* and caching
the blob alongside the index is the natural shape:

* The index is already per-root (``.snapctx/index.db``), so the cache
  inherits the same locality. One preloads table per root, no
  ``root_path`` column needed.
* The cache must outlive the TUI process — opening the same root
  later should hit the same map without re-paying for it.
* When the codebase changes, the cached map is by definition stale.
  The write primitives in ``snapctx.api`` (``edit_symbol``,
  ``edit_symbol_search_replace``, ``add_import``, …) call
  ``invalidate_preloads`` on every successful operation, so a single
  read primitive can never serve a map that disagrees with the
  current source tree.

The "version" we stamp each row with is just a SHA-1 over the
``(path, sha)`` tuples in the ``files`` table, sorted. It costs one
SQL query and a hash — no extra I/O — and is bit-identical across
processes, so two TUIs talking to the same repo agree on hits/misses.

This module owns three public APIs and one internal helper:

* ``current_source_version(root)`` — version stamp for "right now"
* ``get_preload(root, mode)`` — return blob if version still matches
* ``set_preload(root, mode, content)`` — write blob with current stamp
* ``invalidate_preloads(root)`` — drop everything (called by writes)
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

from snapctx.api._common import open_index


def current_source_version(root: Path) -> str:
    """Return a stable hash of every ``(path, sha)`` row in the index.

    The hash is SHA-1 over a sorted, newline-separated rendering of
    each ``files`` row — same input bytes always produce the same
    digest, so two processes opening the same index agree on the
    version. Cheap: one indexed scan of the ``files`` table, no
    filesystem I/O (the SHAs already live in the index).

    Args:
        root: Repo root passed to ``index_root``. The function opens
            the corresponding ``.snapctx/index.db`` read-only.

    Returns:
        Hex-encoded SHA-1 (40 chars). Stable for an unchanged index;
        any change to the indexed files (add / remove / modify) flips
        the digest.
    """
    idx = open_index(Path(root), scope=None)
    try:
        rows = idx.conn.execute(
            "SELECT path, sha FROM files ORDER BY path"
        ).fetchall()
    finally:
        idx.close()
    h = hashlib.sha1()
    for r in rows:
        h.update(r["path"].encode("utf-8"))
        h.update(b"\0")
        h.update(r["sha"].encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


def get_preload(root: Path, mode: str) -> str | None:
    """Return the cached preload blob for ``mode`` if it's still fresh.

    "Fresh" means the row's ``source_version`` matches the current
    ``current_source_version(root)``. A mismatch is treated as a miss
    even though the row physically exists — this lets us trade one
    SHA-1 over the files table for the certainty that we never
    serve a map that disagrees with the source tree.

    Args:
        root: Repo root the preload was written under.
        mode: Opaque key the caller picks. The cache treats it as a
            bare string — ``"fast"``, ``"balanced"``, etc.

    Returns:
        The stored ``content`` blob, or ``None`` on a cold miss
        (no row) or a stale hit (row exists but ``source_version``
        no longer matches).
    """
    idx = open_index(Path(root), scope=None)
    try:
        row = idx.conn.execute(
            "SELECT content, source_version FROM preloads WHERE mode = ?",
            (mode,),
        ).fetchone()
    finally:
        idx.close()
    if row is None:
        return None
    if row["source_version"] != current_source_version(root):
        return None
    return row["content"]


def set_preload(root: Path, mode: str, content: str) -> None:
    """Write a fresh preload blob for ``mode``, stamped with the current version.

    Overwrites any existing row for the same mode (last-writer-wins).
    Uses the same WAL connection / busy-timeout as every other write,
    so concurrent calls serialize cleanly instead of raising
    ``database is locked`` (issue #10 pattern).

    Args:
        root: Repo root.
        mode: Opaque key (see ``get_preload``).
        content: The blob to cache. Stored verbatim.
    """
    version = current_source_version(root)
    now = int(time.time())
    idx = open_index(Path(root), scope=None)
    try:
        with idx.tx():
            idx.conn.execute(
                "INSERT OR REPLACE INTO preloads("
                "mode, content, source_version, generated_at"
                ") VALUES (?, ?, ?, ?)",
                (mode, content, version, now),
            )
    finally:
        idx.close()


def invalidate_preloads(root: Path) -> None:
    """Drop every cached preload row for ``root``.

    Called automatically by every successful write primitive in
    ``snapctx.api`` (``edit_symbol``, ``edit_symbol_search_replace``,
    ``add_import``, …) so the next ``get_preload`` after a mutation
    is guaranteed to miss. Callers normally don't need to invoke
    this themselves.

    No-op if the index doesn't exist yet — a fresh root that hasn't
    been indexed has nothing to invalidate.
    """
    from snapctx.index import db_path_for

    db = db_path_for(Path(root), scope=None)
    if not db.exists():
        return
    idx = open_index(Path(root), scope=None)
    try:
        with idx.tx():
            idx.conn.execute("DELETE FROM preloads")
    finally:
        idx.close()
