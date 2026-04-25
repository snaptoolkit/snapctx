"""Auto-discovery of indexed roots.

snapctx is meant to be invoked from anywhere in a project tree. The CLI
shouldn't require ``--root <path>`` every time, and when launched from a
parent that contains multiple indexed sub-projects (e.g. a monorepo with
``backend/`` Python and ``frontend/`` TS each indexed separately), it
should fan out queries across all of them.

The rules:

1. **Walk up** from the start path. If any ancestor (or the start itself)
   contains ``.snapctx/index.db``, that's the single root. Done.
2. **Walk down** one level if the walk-up found nothing. Each immediate
   child that contains ``.snapctx/index.db`` becomes a root. Returning
   multiple roots signals "multi-root mode" to callers.
3. **Empty list** if neither step finds anything — the caller surfaces
   the "no index found, run `snapctx index` first" error.

Walk-down is deliberately shallow (one level). Two-level walk-down is
rarely needed in practice and the cost of scanning a deep ``node_modules``
or ``.venv`` for ``.snapctx`` dirs isn't worth it. A user with apps nested
under ``apps/web``, ``apps/api`` should index from ``apps/`` or invoke
each separately.
"""

from __future__ import annotations

from pathlib import Path

from snapctx.index import db_path_for

_MAX_WALK_UP = 20  # Hard ceiling — we should never need this many levels.


def discover_roots(start: str | Path) -> list[Path]:
    """Find indexed roots reachable from ``start``.

    Returns:
      * ``[root]`` if a ``.snapctx/index.db`` exists at ``start`` or any
        ancestor (the closest one wins).
      * ``[child1, child2, ...]`` if no enclosing index exists but one or
        more immediate child directories of ``start`` are indexed.
      * ``[]`` if nothing is found.

    All returned paths are absolute and resolved.
    """
    start_path = Path(start).resolve()
    if start_path.is_file():
        start_path = start_path.parent

    # 1. Walk up: nearest enclosing index wins.
    current = start_path
    for _ in range(_MAX_WALK_UP):
        if db_path_for(current).exists():
            return [current]
        if current.parent == current:
            break
        current = current.parent

    # 2. Walk down one level: collect every indexed child.
    roots: list[Path] = []
    if start_path.is_dir():
        try:
            children = sorted(start_path.iterdir())
        except OSError:
            return []
        for child in children:
            if not child.is_dir() or child.name.startswith("."):
                continue
            if db_path_for(child).exists():
                roots.append(child.resolve())
    return roots


def root_label(root: Path, anchor: Path | None = None) -> str:
    """Short human-readable name for a root, used to tag multi-root results.

    Falls back to the root's own name if no anchor is given or if the root
    isn't a descendant of the anchor.
    """
    if anchor is not None:
        try:
            rel = root.relative_to(anchor)
            s = str(rel)
            if s and s != ".":
                return s
        except ValueError:
            pass
    return root.name


def route_by_qname(qname: str, roots: list[Path]) -> Path | None:
    """Return the first root whose index contains ``qname``.

    Used by qname-based ops (``expand``, ``source``) in multi-root mode.
    Returns None if no root has the symbol.
    """
    from snapctx.index import Index

    for r in roots:
        db = db_path_for(r)
        if not db.exists():
            continue
        idx = Index(db)
        try:
            row = idx.get_symbol(qname)
            if row is not None:
                return r
        finally:
            idx.close()
    return None


def route_by_path(target: str | Path, roots: list[Path]) -> Path | None:
    """Return the root whose directory is the longest prefix of ``target``.

    Used by path-based ops (``outline``) in multi-root mode. The target
    is resolved to an absolute path before matching.
    """
    target_path = Path(target)
    if not target_path.is_absolute():
        # Path is relative to *something*. We can't pick a root from a
        # relative path alone — let the caller try each root.
        return None
    target_path = target_path.resolve()

    best: Path | None = None
    best_len = -1
    for r in roots:
        try:
            target_path.relative_to(r)
        except ValueError:
            continue
        rl = len(str(r))
        if rl > best_len:
            best = r
            best_len = rl
    return best
