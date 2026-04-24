"""Command-line entry point: `neargrep <subcommand>`."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from neargrep.api import context, expand, get_source, index_root, outline, search_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="neargrep", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_index = sub.add_parser("index", help="Scan a repo and build/update the index.")
    p_index.add_argument("root", nargs="?", default=".", help="Repo root (default: cwd)")

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
    p_context.add_argument("--mode", choices=["lexical", "vector", "hybrid"], default="hybrid")
    p_context.add_argument("--kind", default=None)
    p_context.add_argument("--root", default=".")

    args = parser.parse_args(argv)

    if args.cmd == "index":
        print(json.dumps(index_root(args.root), indent=2))
        return 0
    if args.cmd == "search":
        print(json.dumps(
            search_code(
                args.query, k=args.k, kind=args.kind, root=args.root, mode=args.mode
            ),
            indent=2,
        ))
        return 0
    if args.cmd == "expand":
        print(json.dumps(
            expand(args.qname, direction=args.direction, depth=args.depth, root=args.root),
            indent=2,
        ))
        return 0
    if args.cmd == "outline":
        print(json.dumps(outline(args.path, root=args.root), indent=2))
        return 0
    if args.cmd == "source":
        print(json.dumps(
            get_source(args.qname, with_neighbors=args.with_neighbors, root=args.root),
            indent=2,
        ))
        return 0
    if args.cmd == "context":
        print(json.dumps(
            context(
                args.query,
                k_seeds=args.k_seeds,
                source_for_top=args.source_for_top,
                mode=args.mode,
                kind=args.kind,
                root=args.root,
            ),
            indent=2,
        ))
        return 0

    parser.error(f"unknown command: {args.cmd}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
