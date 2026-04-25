"""Command-line entry point: `snapctx <subcommand>`.

The CLI is meant to be runnable from anywhere in a project tree:

* From a directory inside an indexed repo, ``--root`` defaults to ``.``
  and we walk up to the nearest enclosing ``.snapctx/index.db``.
* From a parent that contains several indexed sub-projects (e.g. a
  monorepo with ``backend/`` and ``frontend/`` each indexed
  separately), queries fan out across all of them and results are
  tagged with which sub-project they came from.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from snapctx.api import (
    context,
    context_multi,
    expand,
    expand_multi,
    get_source,
    get_source_multi,
    index_root,
    outline,
    outline_multi,
    search_code,
    search_code_multi,
)
from snapctx.roots import discover_roots, root_label
from snapctx.watch import run_watch


def _resolve_roots(start: str) -> tuple[list[Path], Path]:
    """Discover indexed roots reachable from ``start``.

    Returns ``(roots, anchor)`` — the anchor is the directory the user
    invoked the command from, used for relative ``root`` labels in the
    response.
    """
    anchor = Path(start).resolve()
    if anchor.is_file():
        anchor = anchor.parent
    return discover_roots(anchor), anchor


def _auto_index(anchor: Path) -> list[Path]:
    """Index ``anchor`` from scratch and re-run discovery.

    Called when a query command finds no existing index. Performs a cheap
    pre-flight check first — if the directory has no source files we can
    parse, return an empty list (caller surfaces an error) so we don't
    leave a stub ``.snapctx/`` behind in unrelated directories.

    All progress messages go to stderr; the query JSON stays clean on
    stdout.
    """
    from snapctx.config import load_config
    from snapctx.walker import iter_source_files

    cfg = load_config(anchor)
    try:
        next(iter(iter_source_files(anchor, cfg.walker)))
    except StopIteration:
        sys.stderr.write(
            f"No snapctx index near {anchor} and no source files to index.\n"
            f"  snapctx indexes Python (.py, .pyi) and TypeScript (.ts, .tsx, .js, .jsx).\n"
            f"  Run from a directory containing source code, or pass a path with `snapctx index <path>`.\n"
        )
        return []

    sys.stderr.write(
        f"No snapctx index at {anchor} — building one now (one-time cost; "
        f"subsequent queries are fast).\n"
    )
    try:
        summary = index_root(anchor)
    except Exception as e:
        sys.stderr.write(f"Auto-index failed: {type(e).__name__}: {e}\n")
        return []
    sys.stderr.write(
        f"Indexed {summary['symbols_indexed']} symbols across "
        f"{summary['files_updated']} files. Querying...\n\n"
    )
    return discover_roots(anchor)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="snapctx", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_index = sub.add_parser("index", help="Scan a repo and build/update the index.")
    p_index.add_argument("root", nargs="?", default=".", help="Repo root (default: cwd)")

    p_watch = sub.add_parser(
        "watch",
        help="Watch a repo and re-index automatically on file save (debounced).",
    )
    p_watch.add_argument("root", nargs="?", default=".", help="Repo root (default: cwd)")
    p_watch.add_argument(
        "--debounce", type=float, default=0.5,
        help="Seconds to wait after the last event before re-indexing (default 0.5).",
    )

    p_search = sub.add_parser("search", help="Search symbols (lexical / vector / hybrid).")
    p_search.add_argument("query")
    p_search.add_argument("-k", type=int, default=5)
    p_search.add_argument("--kind", default=None)
    p_search.add_argument(
        "--mode", choices=["lexical", "vector", "hybrid"], default="hybrid",
        help="Ranker. hybrid = RRF of FTS5 + embeddings (default).",
    )
    p_search.add_argument("--root", default=".")

    p_expand = sub.add_parser("expand", help="Walk the call graph around a qname.")
    p_expand.add_argument("qname")
    p_expand.add_argument(
        "--direction", choices=["callees", "callers", "both"], default="callees"
    )
    p_expand.add_argument("--depth", type=int, default=1)
    p_expand.add_argument("--root", default=".")

    p_outline = sub.add_parser("outline", help="Show the symbol tree of a file.")
    p_outline.add_argument("path")
    p_outline.add_argument("--root", default=".")

    p_source = sub.add_parser("source", help="Show the source of a single symbol.")
    p_source.add_argument("qname")
    p_source.add_argument("--with-neighbors", action="store_true")
    p_source.add_argument("--root", default=".")

    p_context = sub.add_parser(
        "context",
        help="One-shot: search + callees + callers + source for top seeds, all in one call.",
    )
    p_context.add_argument("query")
    p_context.add_argument("--k-seeds", type=int, default=5)
    p_context.add_argument("--source-for-top", type=int, default=5)
    p_context.add_argument(
        "--file-outline-limit", type=int, default=8,
        help="Max unique files to outline in the response (default: 8).",
    )
    p_context.add_argument(
        "--outline-discovery-k", type=int, default=15,
        help="Overfetch search to this many candidates for file discovery (default: 15).",
    )
    p_context.add_argument("--mode", choices=["lexical", "vector", "hybrid"], default="hybrid")
    p_context.add_argument("--kind", default=None)
    p_context.add_argument("--root", default=".")

    p_roots = sub.add_parser(
        "roots",
        help="Show which indexed roots snapctx would query from this directory.",
    )
    p_roots.add_argument("root", nargs="?", default=".", help="Start path (default: cwd)")

    args = parser.parse_args(argv)

    # ---- index / watch: per-root, no fan-out. If launched from a parent
    # containing multiple indexed sub-projects, refuse and ask the user
    # to pick one — indexing is a destructive write op and we shouldn't
    # silently iterate over many sub-projects.
    if args.cmd == "index":
        # Indexing creates the .snapctx dir if missing, so no discovery
        # is needed — just operate on the explicit path.
        print(json.dumps(index_root(args.root), indent=2))
        return 0

    if args.cmd == "watch":
        run_watch(Path(args.root), debounce_seconds=args.debounce)
        return 0

    if args.cmd == "roots":
        roots, anchor = _resolve_roots(args.root)
        out = {
            "anchor": str(anchor),
            "roots": [
                {"label": root_label(r, anchor), "path": str(r)}
                for r in roots
            ],
            "mode": "single" if len(roots) == 1 else ("multi" if roots else "none"),
        }
        if not roots:
            out["hint"] = (
                f"No .snapctx/index.db found at or below {anchor}. "
                f"Run a query (e.g. `snapctx context ...`) to auto-index, "
                f"or `snapctx index <path>` explicitly."
            )
        print(json.dumps(out, indent=2))
        return 0 if roots else 1

    # ---- query commands: discover roots, fan out if multiple ----
    roots, anchor = _resolve_roots(args.root)
    if not roots:
        # No index found anywhere reachable. Build one transparently so
        # the user's first query "just works".
        roots = _auto_index(anchor)
        if not roots:
            return 2

    multi = len(roots) > 1

    if args.cmd == "search":
        if multi:
            result = search_code_multi(
                args.query, roots, k=args.k, kind=args.kind,
                mode=args.mode, anchor=anchor,
            )
        else:
            result = search_code(
                args.query, k=args.k, kind=args.kind, root=roots[0], mode=args.mode,
            )
        print(json.dumps(result, indent=2))
        return 0

    if args.cmd == "expand":
        if multi:
            result = expand_multi(
                args.qname, roots, direction=args.direction, depth=args.depth,
                anchor=anchor,
            )
        else:
            result = expand(
                args.qname, direction=args.direction, depth=args.depth, root=roots[0],
            )
        print(json.dumps(result, indent=2))
        return 0

    if args.cmd == "outline":
        if multi:
            result = outline_multi(args.path, roots, anchor=anchor)
        else:
            result = outline(args.path, root=roots[0])
        print(json.dumps(result, indent=2))
        return 0

    if args.cmd == "source":
        if multi:
            result = get_source_multi(
                args.qname, roots, with_neighbors=args.with_neighbors,
                anchor=anchor,
            )
        else:
            result = get_source(
                args.qname, with_neighbors=args.with_neighbors, root=roots[0],
            )
        print(json.dumps(result, indent=2))
        return 0

    if args.cmd == "context":
        if multi:
            result = context_multi(
                args.query, roots,
                k_seeds=args.k_seeds,
                source_for_top=args.source_for_top,
                file_outline_limit=args.file_outline_limit,
                outline_discovery_k=args.outline_discovery_k,
                mode=args.mode,
                kind=args.kind,
                anchor=anchor,
            )
        else:
            result = context(
                args.query,
                k_seeds=args.k_seeds,
                source_for_top=args.source_for_top,
                file_outline_limit=args.file_outline_limit,
                outline_discovery_k=args.outline_discovery_k,
                mode=args.mode,
                kind=args.kind,
                root=roots[0],
            )
        print(json.dumps(result, indent=2))
        return 0

    parser.error(f"unknown command: {args.cmd}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
