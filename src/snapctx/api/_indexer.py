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

    try:
        # Snapshot the current filesystem view and diff it against the DB so
        # rows for files that have been deleted / renamed / .gitignored go
        # away. Without this, stale symbols and call edges accumulate.
        walker_files = {str(f.resolve()) for f in iter_source_files(root_path, cfg.walker)}
        db_files = {row["path"] for row in idx.conn.execute("SELECT path FROM files").fetchall()}
        for stale in db_files - walker_files:
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
