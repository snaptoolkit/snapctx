"""``index_root`` — full repo scan + incremental ingest into the SQLite index.

Steps:

1. Resolve config (``snapctx.toml`` if present, else defaults).
2. Walk the repo for source files; diff against the file table to drop
   stale rows (deleted / renamed / .gitignore'd files).
3. Re-parse only files whose SHA changed; ingest symbols / calls / imports.
4. Post-pass A: demote optimistic callee qnames that didn't resolve.
5. Post-pass B: promote forward-referenced ``self.X`` calls now that
   the full symbol table exists.
6. Post-pass C: embed any newly-added symbols.

Each stage's count flows into the summary returned to the CLI. Only
``index_root`` is exposed; the post-passes belong to ``Index``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

from snapctx.index import Index, db_path_for

_log = logging.getLogger(__name__)


def index_root(
    root: str | Path,
    *,
    force: bool = False,
    progress_callback: Callable[[int, int, str], None] | None = None,
) -> dict:
    """Index (or re-index) every supported source file under ``root``.

    Reads ``<root>/snapctx.toml`` if present to override the walker's
    skip lists, language enable list, or size cap. Without a config
    file, behavior is identical to the pre-config version.

    Incremental by default: files whose SHA matches the stored value are
    skipped. ``force=True`` wipes the existing index first and rebuilds
    from scratch — needed after a parser upgrade (new file types, new
    symbol kinds) so previously-indexed files get re-parsed even when
    their bytes haven't changed.

    ``progress_callback`` (keyword-only) is invoked once per file
    processed inside the main loop — for every updated, skipped, OR
    removed file — with ``(current, total, path)`` where ``current`` is
    1-indexed and ``total`` is fixed for the whole pass
    (``len(walker_files) + len(stale_files)``). Exceptions raised by
    the callback are swallowed (logged at ``DEBUG``) so a misbehaving
    UI can't abort indexing.

    Returns a summary dict with counts.
    """
    from snapctx.config import load_config
    from snapctx.index import sha_bytes
    from snapctx.parsers.registry import parser_for_path
    from snapctx.walker import iter_source_files

    root_path = Path(root).resolve()
    cfg = load_config(root_path)
    idx = Index(db_path_for(root_path))
    counts = {"scanned": 0, "updated": 0, "skipped": 0, "symbols": 0, "removed": 0}
    moved = False
    parser_version_rebuilt = False

    try:
        if force:
            idx.wipe_all()
        # Parser-version drift: SHA-keyed incremental indexing skips
        # files whose bytes haven't changed, but a parser upgrade can
        # change what gets emitted for the SAME bytes (e.g. issue #21
        # made module symbols mandatory). When the stored version
        # doesn't match the current parser, wipe so the walk below
        # reparses every file against fresh code instead of leaving
        # the old rows in place forever.
        if idx.parser_version_outdated:
            idx.wipe_all()
            parser_version_rebuilt = True
        # If the project was renamed/moved on disk, every stored absolute
        # path is now stale. Detect via a sample row's prefix and wipe so
        # the rebuild below repopulates with current paths. Cheaper than
        # rewriting every row in-place, and avoids the path-mismatch trap
        # where the staleness diff would forget every old row anyway —
        # except the auto-refresh transaction would then hit the explicit-
        # BEGIN-vs-implicit-BEGIN race that this commit also fixes.
        moved = _wipe_if_root_moved(idx, root_path)

        # Snapshot the current filesystem view and diff it against the DB so
        # rows for files that have been deleted / renamed / .gitignored go
        # away. Without this, stale symbols and call edges accumulate.
        walker_files = {str(f.resolve()) for f in iter_source_files(root_path, cfg.walker)}
        db_files = {row["path"] for row in idx.conn.execute("SELECT path FROM files").fetchall()}
        stale_files = db_files - walker_files
        # Stable per-pass denominator: every file the loop will touch
        # exactly once (removals + walker hits). Computed up front so the
        # UI can render a fixed-width bar.
        total = len(walker_files) + len(stale_files)
        current = 0

        def _report(path: str) -> None:
            """Forward (current, total, path) to ``progress_callback`` if
            one was supplied. Swallow + log any exception so a buggy
            consumer can't break the index pass."""
            if progress_callback is None:
                return
            try:
                progress_callback(current, total, path)
            except Exception:
                _log.debug("progress_callback raised; ignoring", exc_info=True)

        for stale in stale_files:
            idx.forget_file(stale)
            counts["removed"] += 1
            current += 1
            _report(stale)

        for file_str in walker_files:
            file = Path(file_str)
            counts["scanned"] += 1
            data = file.read_bytes()
            sha = sha_bytes(data)
            if idx.current_sha(file_str) == sha:
                counts["skipped"] += 1
                current += 1
                _report(file_str)
                continue
            parser = parser_for_path(file)
            assert parser is not None   # walker already filtered
            result = parser.parse(file, root_path)
            idx.ingest(file_str, parser.language, sha, result)
            counts["updated"] += 1
            counts["symbols"] += len(result.symbols)
            current += 1
            _report(file_str)

        # Post-pass A — demote unresolved optimistic callees first, so
        # promote_self_calls (B) and promote_imported_calls (C) see a
        # clean slate of None callee_qnames to fill in. Order matters.
        demoted = idx.demote_unresolved_calls()
        idx.promote_self_calls()
        idx.promote_imported_calls()
        embedded = _embed_missing(idx)
        idx.stamp_parser_version()
    finally:
        idx.close()

    # Post-pass D — extract framework routes (Django urls.py + Next.js
    # App Router). Cheap; routes are a small fraction of files in any
    # real project. Done after the main idx is closed because route
    # extraction opens its own short-lived connection per call.
    from snapctx.api._routes import reextract_all_routes
    routes_indexed = reextract_all_routes(root_path)

    return {
        "root": str(root_path),
        "files_scanned": counts["scanned"],
        "files_updated": counts["updated"],
        "files_unchanged": counts["skipped"],
        "files_removed": counts["removed"],
        "symbols_indexed": counts["symbols"],
        "calls_demoted": demoted,
        "symbols_embedded": embedded,
        "routes_indexed": routes_indexed,
        "root_moved": moved,
        "parser_version_rebuilt": parser_version_rebuilt,
    }


def _wipe_if_root_moved(idx: Index, root_path: Path) -> bool:
    """Drop all rows when stored paths don't sit under the current root.

    Heuristic: look at one ``files.path`` row. If it isn't a child of
    ``root_path``, the project was renamed/moved (or the index was copied
    into a sibling repo). Wiping is cheaper than rewriting every absolute
    path in symbols/calls/imports/files.
    """
    sample = idx.conn.execute("SELECT path FROM files LIMIT 1").fetchone()
    if sample is None:
        return False
    sample_path = Path(sample["path"])
    try:
        sample_path.relative_to(root_path)
    except ValueError:
        idx.wipe_all()
        return True
    return False


def index_vendor_package(
    repo_root: str | Path, name: str, pkg_path: str | Path
) -> dict:
    """Ingest one third-party package into its own dedicated index.

    Storage: ``<repo_root>/.snapctx/vendor/<name>/index.db``. Completely
    isolated from the repo's index — separate symbols, separate FTS, and
    most importantly separate vector matrix so cosine search inside the
    package isn't polluted by cross-namespace neighbors.

    The parser is rooted at ``pkg_path`` (not ``repo_root``), so qnames
    inside this index look like ``db.models.query:QuerySet`` — the
    package's own module structure — instead of carrying a long
    ``.venv.lib.python*.site-packages.django.…`` prefix.
    """
    from snapctx.config import WalkerConfig
    from snapctx.index import sha_bytes
    from snapctx.parsers.registry import parser_for_path
    from snapctx.walker import iter_source_files

    repo_root = Path(repo_root).resolve()
    pkg_path = Path(pkg_path).resolve()
    idx = Index(db_path_for(repo_root, scope=name))
    counts = {"updated": 0, "skipped": 0, "symbols": 0}

    # Inside a venv / node_modules: gitignore would block us, vendor-skip
    # would block us — disable both. Bundle filter stays on so a
    # ``react/dist/react.min.js`` doesn't pollute the index.
    cfg = WalkerConfig(
        skip_vendor_packages=False,
        respect_gitignore=False,
    )

    try:
        # Same rename-detection trick as the repo index — if the package
        # was reinstalled (uv / pip --upgrade), the absolute paths in our
        # DB no longer resolve. Wipe and rebuild instead of dragging stale
        # rows forward.
        moved = _wipe_if_root_moved(idx, pkg_path)
        if idx.parser_version_outdated:
            idx.wipe_all()

        for file in iter_source_files(pkg_path, cfg):
            file_str = str(file.resolve())
            data = file.read_bytes()
            sha = sha_bytes(data)
            if idx.current_sha(file_str) == sha:
                counts["skipped"] += 1
                continue
            parser = parser_for_path(file)
            assert parser is not None
            result = parser.parse(file, pkg_path)
            idx.ingest(file_str, parser.language, sha, result)
            counts["updated"] += 1
            counts["symbols"] += len(result.symbols)

        demoted = idx.demote_unresolved_calls()
        idx.promote_self_calls()
        idx.promote_imported_calls()
        embedded = _embed_missing(idx)
        idx.stamp_parser_version()
    finally:
        idx.close()

    return {
        "package": name,
        "package_path": str(pkg_path),
        "files_updated": counts["updated"],
        "files_unchanged": counts["skipped"],
        "symbols_indexed": counts["symbols"],
        "calls_demoted": demoted,
        "symbols_embedded": embedded,
        "package_moved": moved,
    }


def _embed_missing(idx: Index) -> int:
    """Embed any symbols added since the last embed pass. Returns count."""
    missing = idx.symbols_without_vectors()
    if not missing:
        return 0
    from snapctx.embeddings import embed_texts, symbol_text_for_embedding

    texts = [
        symbol_text_for_embedding(m["qname"], m["signature"], m["docstring"])
        for m in missing
    ]
    vectors = embed_texts(texts)
    idx.upsert_vectors([m["qname"] for m in missing], vectors)
    return len(missing)
