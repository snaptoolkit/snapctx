"""Query-driven on-demand indexing of installed third-party packages.

The user shouldn't have to remember to run ``snapctx index`` after switching
to a question about Django internals. This module hooks into the query
path: tokenize the user's query, match tokens against package directories
discovered under the project root, and ingest just those packages on the
fly. Indexed packages are tracked in ``indexed_vendor_packages`` so the
second query for the same package is zero-cost.

Discovery is intentionally narrow:

- Python: ``<root>/{.venv,venv,env}/lib/python*/site-packages/<name>/``
- Node:   ``<root>/node_modules/<name>/`` (top-level; scoped ``@x/y``
  packages are intentionally out of scope for v1 — they're a small
  fraction of usage and complicate query matching).

We match query tokens against the *directory name* (which is also the
import name in 99% of cases), not the distribution name from PyPI / npm.
That avoids needing metadata and matches what the user types.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_VENV_DIRS = (".venv", "venv", "env")
_DIST_INFO_SUFFIXES = (".dist-info", ".egg-info", ".data")
_NODE_MODULES = "node_modules"


def discover_packages(root: Path) -> dict[str, Path]:
    """Return ``{name: absolute_path}`` for installed packages under ``root``.

    First match wins on duplicates so a project's primary venv (``.venv``,
    checked first) shadows any stale ``venv/`` left over from a previous
    setup. Caller is expected to filter by query relevance — discovery is
    cheap (one ``glob`` per known location).
    """
    found: dict[str, Path] = {}
    root = root.resolve()

    for venv_name in _VENV_DIRS:
        venv = root / venv_name
        if not venv.is_dir():
            continue
        for site in venv.glob("lib/python*/site-packages"):
            if not site.is_dir():
                continue
            for child in site.iterdir():
                name = child.name
                if not child.is_dir():
                    continue
                if name.startswith((".", "_")):
                    continue
                if any(name.endswith(suf) for suf in _DIST_INFO_SUFFIXES):
                    continue
                if name not in found:
                    found[name] = child.resolve()

    nm = root / _NODE_MODULES
    if nm.is_dir():
        for child in nm.iterdir():
            if not child.is_dir():
                continue
            name = child.name
            if name.startswith((".", "@")):
                continue
            if name not in found:
                found[name] = child.resolve()

    return found


_TOKEN_SPLIT = re.compile(r"[^\w]+")


def query_tokens(query: str) -> set[str]:
    """Lowercased, word-boundary-split tokens from a free-text query."""
    return {t for t in _TOKEN_SPLIT.split(query.lower()) if t}


def match_packages(query: str, packages: dict[str, Path]) -> list[tuple[str, Path]]:
    """Return ``(name, path)`` pairs for packages whose name appears as a
    full token in the query. Case-insensitive, exact token match — no
    fuzzy / substring hits, since "modeling" should not pull in ``model``.
    """
    tokens = query_tokens(query)
    if not tokens:
        return []
    return [(name, path) for name, path in packages.items() if name.lower() in tokens]


def ensure_packages_for_query(
    root: Path,
    query: str,
    *,
    explicit: list[str] | None = None,
    enable_auto: bool = True,
) -> list[dict]:
    """Index any vendor packages referenced by the query that aren't yet indexed.

    ``explicit`` is a user-provided list (CLI ``--pkg``) that bypasses the
    token-match. Auto-detection runs only when ``enable_auto`` is True.

    Returns one summary dict per *newly-indexed* package; already-indexed
    packages are silently skipped (they're still queryable). Progress is
    written to stderr so JSON callers stay clean.
    """
    from snapctx.api._indexer import index_subtree
    from snapctx.index import Index, db_path_for

    targets: list[tuple[str, Path]] = []
    packages = discover_packages(root)
    if explicit:
        for name in explicit:
            if name in packages:
                targets.append((name, packages[name]))
            else:
                sys.stderr.write(
                    f"snapctx: --pkg {name} not found under {root} "
                    f"(checked .venv/, venv/, env/, node_modules/)\n"
                )
    if enable_auto:
        for pair in match_packages(query, packages):
            if pair not in targets:
                targets.append(pair)

    if not targets:
        return []

    idx = Index(db_path_for(root))
    try:
        pending = [(n, p) for n, p in targets if not idx.is_vendor_indexed(n)]
    finally:
        idx.close()

    summaries: list[dict] = []
    for name, path in pending:
        sys.stderr.write(
            f"snapctx: indexing vendor package {name} at {path} (one-time)...\n"
        )
        summary = index_subtree(root, path)
        summary["package"] = name
        summaries.append(summary)
        # Mark as indexed in a fresh connection (index_subtree closed its own).
        idx = Index(db_path_for(root))
        try:
            idx.mark_vendor_indexed(name, str(path))
        finally:
            idx.close()
        sys.stderr.write(
            f"snapctx: vendor package {name} ready "
            f"({summary['files_updated']} files, {summary['symbols_indexed']} symbols).\n"
        )
    return summaries
