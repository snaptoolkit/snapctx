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

from pathlib import Path

from snapctx.index import Index, db_path_for


def index_root(root: str | Path) -> dict:
    """Index (or re-index) every supported source file under ``root``.

    Reads ``<root>/snapctx.toml`` if present to override the walker's
    skip lists, language enable list, or size cap. Without a config
    file, behavior is identical to the pre-config version.

    Incremental: files whose SHA matches the stored value are skipped.
    Returns a summary dict with counts.
    """
    from snapctx.config import load_config
    from snapctx.index import sha_bytes
    from snapctx.parsers.registry import parser_for
    from snapctx.walker import iter_source_files

    root_path = Path(root).resolve()
    cfg = load_config(root_path)
    idx = Index(db_path_for(root_path))
    counts = {"scanned": 0, "updated": 0, "skipped": 0, "symbols": 0, "removed": 0}
    moved = False

    try:
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
        # Files brought in by on-demand vendor indexing live outside the
        # walker's view (they're under .venv / node_modules, gitignored).
        # Without this guard, every regular re-index would forget them —
        # and the next vendor query would have to ingest django from scratch.
        vendor_prefixes = tuple(p.rstrip("/") + "/" for p in idx.vendor_paths())
        for stale in db_files - walker_files:
            if vendor_prefixes and stale.startswith(vendor_prefixes):
                continue
            idx.forget_file(stale)
            counts["removed"] += 1

        for file_str in walker_files:
            file = Path(file_str)
            counts["scanned"] += 1
            data = file.read_bytes()
            sha = sha_bytes(data)
            if idx.current_sha(file_str) == sha:
                counts["skipped"] += 1
                continue
            parser = parser_for(file.suffix)
            assert parser is not None   # walker already filtered
            result = parser.parse(file, root_path)
            idx.ingest(file_str, parser.language, sha, result)
            counts["updated"] += 1
            counts["symbols"] += len(result.symbols)

        # Post-pass A — demote unresolved optimistic callees first, so
        # promote_self_calls (B) sees a clean slate of None callee_qnames
        # to fill in. Order matters.
        demoted = idx.demote_unresolved_calls()
        idx.promote_self_calls()
        embedded = _embed_missing(idx)
    finally:
        idx.close()

    return {
        "root": str(root_path),
        "files_scanned": counts["scanned"],
        "files_updated": counts["updated"],
        "files_unchanged": counts["skipped"],
        "files_removed": counts["removed"],
        "symbols_indexed": counts["symbols"],
        "calls_demoted": demoted,
        "symbols_embedded": embedded,
        "root_moved": moved,
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


def index_subtree(project_root: str | Path, subtree: str | Path) -> dict:
    """Ingest one subtree into the project's index without touching unrelated rows.

    Used for on-demand vendor-package indexing: the caller has matched a
    query token against an installed package and wants just *that* package
    pulled into the existing index. Differs from ``index_root`` in two
    ways: (1) walks only the subtree, and (2) skips the staleness diff
    entirely — the rest of the project's files are still in the DB and
    we must not forget them.

    Parsers receive ``project_root`` for qname computation so symbols
    indexed here share the same namespace as the rest of the project
    (``.venv.lib.python3.14.site-packages.django.db.models.query:QuerySet``).
    """
    from snapctx.config import WalkerConfig
    from snapctx.index import sha_bytes
    from snapctx.parsers.registry import parser_for
    from snapctx.walker import iter_source_files

    project_root = Path(project_root).resolve()
    subtree = Path(subtree).resolve()
    idx = Index(db_path_for(project_root))
    counts = {"updated": 0, "skipped": 0, "symbols": 0}

    # Vendor-package code: don't honor gitignore (the venv is gitignored
    # by definition), don't skip vendor dirs (we're inside one), keep the
    # bundle filter (.min.js inside node_modules is still noise).
    cfg = WalkerConfig(
        skip_vendor_packages=False,
        respect_gitignore=False,
    )

    try:
        for file in iter_source_files(subtree, cfg):
            file_str = str(file.resolve())
            data = file.read_bytes()
            sha = sha_bytes(data)
            if idx.current_sha(file_str) == sha:
                counts["skipped"] += 1
                continue
            parser = parser_for(file.suffix)
            assert parser is not None
            result = parser.parse(file, project_root)
            idx.ingest(file_str, parser.language, sha, result)
            counts["updated"] += 1
            counts["symbols"] += len(result.symbols)

        demoted = idx.demote_unresolved_calls()
        idx.promote_self_calls()
        embedded = _embed_missing(idx)
    finally:
        idx.close()

    return {
        "subtree": str(subtree),
        "files_updated": counts["updated"],
        "files_unchanged": counts["skipped"],
        "symbols_indexed": counts["symbols"],
        "calls_demoted": demoted,
        "symbols_embedded": embedded,
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
