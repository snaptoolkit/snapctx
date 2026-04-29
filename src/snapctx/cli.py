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
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from snapctx.api import (
    add_import,
    add_import_multi,
    context,
    context_multi,
    delete_symbol,
    delete_symbol_multi,
    edit_symbol,
    edit_symbol_multi,
    expand,
    expand_multi,
    find_literal,
    find_literal_multi,
    get_source,
    get_source_multi,
    grep_files,
    grep_files_multi,
    index_root,
    insert_symbol,
    insert_symbol_multi,
    map_repo,
    map_repo_multi,
    outline,
    outline_multi,
    remove_import,
    remove_import_multi,
    search_code,
    search_code_multi,
)
from snapctx.roots import discover_roots, find_subproject_dirs, has_project_marker, root_label
from snapctx.vendor import (
    discover_packages,
    ensure_vendor_indexed,
    forget_vendor,
    list_indexed_vendors,
    parse_query_prefix,
)
from snapctx.watch import run_watch


# ---------- query-command registry ----------
#
# Each query command — search, expand, outline, source, context — has the
# same shape: parse args → discover roots → call single or multi version →
# print JSON. The registry below captures the per-command bits (which
# functions to call, which argparse fields to forward) so the dispatch
# loop is one line per command.


@dataclass(frozen=True)
class QueryCommand:
    """A query command's per-instance config.

    ``arg_names`` are argparse attribute names (after argparse's dash→
    underscore conversion) to pull off ``args`` and forward as kwargs.
    Both ``single`` and ``multi`` accept these as kwargs; ``single`` also
    accepts ``root=Path``, ``multi`` accepts ``roots=list[Path]`` plus
    ``anchor=Path``.
    """

    name: str
    single: Callable[..., dict]
    multi: Callable[..., dict]
    arg_names: tuple[str, ...]

    def call(self, args: argparse.Namespace, roots: list[Path], anchor: Path) -> dict:
        kwargs = {a: getattr(args, a) for a in self.arg_names}
        scope = getattr(args, "scope", None)
        if len(roots) > 1:
            if scope is not None:
                # v1 simplification: vendor packages are per-root, and we
                # don't have a meaningful "merge X across N vendor indexes"
                # story yet. Surface the conflict explicitly.
                raise SystemExit(
                    f"snapctx: vendor scope ({scope!r}) is not supported across "
                    f"multiple roots. Run from inside one of: "
                    f"{', '.join(str(r) for r in roots)}"
                )
            return self.multi(roots=roots, anchor=anchor, **kwargs)
        return self.single(root=roots[0], scope=scope, **kwargs)


_QUERY_COMMANDS: tuple[QueryCommand, ...] = (
    QueryCommand("search", search_code, search_code_multi,
                 arg_names=("query", "k", "kind", "mode", "with_bodies", "also")),
    QueryCommand("expand", expand, expand_multi,
                 arg_names=("qname", "direction", "depth")),
    QueryCommand("outline", outline, outline_multi,
                 arg_names=("path", "max_files", "with_bodies")),
    QueryCommand("source", get_source, get_source_multi,
                 arg_names=("qname", "with_neighbors")),
    QueryCommand("context", context, context_multi,
                 arg_names=(
                     "query", "k_seeds", "source_for_top",
                     "file_outline_limit", "outline_discovery_k",
                     "mode", "kind",
                 )),
    QueryCommand("find", find_literal, find_literal_multi,
                 arg_names=(
                     "literal", "in_path", "kind",
                     "with_bodies", "with_callers", "max_results",
                 )),
    QueryCommand("grep", grep_files, grep_files_multi,
                 arg_names=(
                     "pattern", "regex", "in_path", "case_insensitive",
                     "context_lines", "max_results", "max_files",
                     "definitions_first",
                 )),
    QueryCommand("map", map_repo, map_repo_multi,
                 arg_names=("depth", "prefix", "mode")),
)
_QUERY_BY_NAME: dict[str, QueryCommand] = {c.name: c for c in _QUERY_COMMANDS}


# ---------- discovery + auto-indexing ----------


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


def _bootstrap_first_index(anchor: Path) -> list[Path]:
    """Build a fresh index when nothing reachable from ``anchor`` is indexed.

    When ``anchor`` itself has no project marker but two or more of its
    immediate children do, treat it as a monorepo parent and auto-index
    each child as a separate root (multi-root). Otherwise fall back to
    indexing ``anchor`` itself.

    Pre-flight check first — if the chosen target(s) have no source
    files we can parse, return an empty list so we don't leave a stub
    ``.snapctx/`` behind in unrelated directories.

    All progress messages go to stderr; the query JSON stays clean on
    stdout. The first-ever invocation also triggers a fastembed model
    download (~30 MB) which prints its own progress.
    """
    if not has_project_marker(anchor):
        subs = find_subproject_dirs(anchor)
        if len(subs) >= 2:
            return _bootstrap_subprojects(anchor, subs)

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
        f"snapctx: building first index at {anchor} "
        f"(downloads embedding model on first use; subsequent queries reuse it)...\n"
    )
    try:
        summary = index_root(anchor)
    except Exception as e:
        sys.stderr.write(f"snapctx: first index failed: {type(e).__name__}: {e}\n")
        return []
    sys.stderr.write(
        f"snapctx: indexed {summary['symbols_indexed']} symbols across "
        f"{summary['files_updated']} files.\n"
    )
    return discover_roots(anchor)


def _bootstrap_subprojects(anchor: Path, subs: list[Path]) -> list[Path]:
    """Auto-index each sub-project under a monorepo parent.

    Sequential to keep the embedding-model loader simple (one fastembed
    instance reused across roots). Failures on individual roots don't
    abort — others still succeed and the caller queries what's available.
    """
    sys.stderr.write(
        f"snapctx: detected monorepo parent at {anchor} — indexing "
        f"{len(subs)} sub-project(s) ({', '.join(s.name for s in subs)})...\n"
    )
    indexed: list[Path] = []
    for sub in subs:
        try:
            summary = index_root(sub)
        except Exception as e:
            sys.stderr.write(
                f"snapctx: index failed at {sub.name}/: {type(e).__name__}: {e}\n"
            )
            continue
        sys.stderr.write(
            f"snapctx: indexed {sub.name}/ — "
            f"{summary['symbols_indexed']} symbols across "
            f"{summary['files_updated']} files.\n"
        )
        indexed.append(sub.resolve())
    return indexed


def _extend_with_subprojects(anchor: Path, roots: list[Path]) -> list[Path]:
    """Auto-index sibling sub-projects when discovery returned a walk-down hit.

    If ``discover_roots`` walked down and found one (or more) indexed
    children, other immediate-child sub-projects with project markers but
    no ``.snapctx/`` would otherwise stay invisible — that's the bug
    where running from a monorepo parent only sees the first sub-project
    that happened to be indexed. Scan for missing siblings, index them,
    and return the combined list.

    Walk-up case (``anchor`` itself or an ancestor is indexed) → no-op:
    the user has a single canonical index covering the tree.
    """
    if not roots:
        return roots
    anchor_resolved = anchor.resolve()
    for r in roots:
        try:
            anchor_resolved.relative_to(r.resolve())
            return roots  # walk-up: at least one root is anchor or above
        except ValueError:
            continue

    existing = {r.resolve() for r in roots}
    new_roots: list[Path] = []
    for sub in find_subproject_dirs(anchor):
        if sub in existing:
            continue
        sys.stderr.write(f"snapctx: auto-indexing sibling sub-project {sub.name}/...\n")
        try:
            summary = index_root(sub)
        except Exception as e:
            sys.stderr.write(
                f"snapctx: auto-index failed at {sub.name}/: {type(e).__name__}: {e}\n"
            )
            continue
        sys.stderr.write(
            f"snapctx: indexed {sub.name}/ — "
            f"{summary['symbols_indexed']} symbols across "
            f"{summary['files_updated']} files.\n"
        )
        new_roots.append(sub)
    return roots + new_roots


def _refresh_indexes(roots: list[Path]) -> None:
    """Run an incremental re-index on every discovered root before querying.

    The index is SHA-keyed, so a no-op re-index is fast (~600 ms on a
    cold CLI; near-zero in a warm process). When source files have
    changed since the last query, this picks up the deltas transparently
    so the user's query always reflects the current code.

    Quiet on no-op (the latency itself signals "we checked"). One-line
    summary on stderr when files were re-parsed or removed.
    """
    multi = len(roots) > 1
    for r in roots:
        try:
            summary = index_root(r)
        except Exception as e:
            sys.stderr.write(
                f"snapctx: re-index failed at {r}: {type(e).__name__}: {e}\n"
            )
            continue
        updated = summary["files_updated"]
        removed = summary["files_removed"]
        rebuilt = summary.get("parser_version_rebuilt", False)
        label = f" ({r.name})" if multi else ""
        if rebuilt:
            sys.stderr.write(
                f"snapctx: parser upgraded since last index{label} — "
                f"rebuilt from scratch ({updated} files re-parsed).\n"
            )
            continue
        if not (updated or removed):
            continue
        parts = []
        if updated:
            parts.append(f"{updated} updated")
        if removed:
            parts.append(f"{removed} removed")
        sys.stderr.write(
            f"snapctx: refreshed index{label} — {', '.join(parts)}\n"
        )


# ---------- argparse setup ----------


def _add_vendor_args(p: argparse.ArgumentParser) -> None:
    """Attach the vendor-scope flag shared by every query command.

    ``--pkg <name>`` is the explicit form of the ``<pkg>:`` query prefix:
    route this query to the per-package index for ``<name>`` instead of
    the repo's. Useful when the operation doesn't take a free-text query
    (``outline``, ``source``, ``expand``) so the prefix-in-query trick
    doesn't apply.
    """
    p.add_argument(
        "--pkg", default=None, metavar="NAME",
        help="Run against the named vendor package's index (e.g. --pkg django).",
    )


def _emit(data) -> None:
    """Print a JSON payload with formatting chosen for the consumer.

    Agents and pipes don't benefit from pretty-printing — every newline
    and indent is pure token overhead, and on a mid-sized repo's ``map``
    the pretty form is ~40% bigger than compact for zero readability
    gain when the consumer is an LLM or ``jq``. Default to compact
    whenever stdout isn't a TTY (i.e. piped or captured); pretty when a
    human is reading the output directly. Both defaults are overridable.
    """
    if _OUTPUT_STYLE == "pretty":
        sys.stdout.write(json.dumps(data, indent=2))
    elif _OUTPUT_STYLE == "compact":
        sys.stdout.write(json.dumps(data, separators=(",", ":")))
    else:
        # auto: pretty for humans, compact for everything else.
        if sys.stdout.isatty():
            sys.stdout.write(json.dumps(data, indent=2))
        else:
            sys.stdout.write(json.dumps(data, separators=(",", ":")))
    sys.stdout.write("\n")


_OUTPUT_STYLE: str = "auto"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="snapctx", description=__doc__)
    output_grp = parser.add_mutually_exclusive_group()
    output_grp.add_argument(
        "--compact", dest="output_style", action="store_const", const="compact",
        help="Force single-line JSON output (default when stdout is piped — saves ~40% bytes).",
    )
    output_grp.add_argument(
        "--pretty", dest="output_style", action="store_const", const="pretty",
        help="Force indented JSON output (default when stdout is a TTY).",
    )
    parser.set_defaults(output_style="auto")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_index = sub.add_parser("index", help="Scan a repo and build/update the index.")
    p_index.add_argument("root", nargs="?", default=".", help="Repo root (default: cwd)")
    p_index.add_argument(
        "--force", "-f", action="store_true",
        help="Wipe the existing index and rebuild from scratch (use after a parser upgrade).",
    )

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
    p_search.add_argument(
        "--with-bodies", dest="with_bodies", action="store_true",
        help=(
            "Inline each hit's source body so audit-style 'list every X' "
            "queries get all the source they need in one call. Pair with "
            "a higher -k (e.g. -k 20)."
        ),
    )
    p_search.add_argument(
        "--also", action="append", default=[], metavar="TERM",
        help=(
            "Add another search term, repeatable. ``--also openai --also "
            "gemini`` runs three searches in one call and merges the "
            "results — ideal for cross-cutting audits over multiple "
            "keywords. Top-k applies to the merged result set."
        ),
    )
    p_search.add_argument("--root", default=".")
    _add_vendor_args(p_search)

    p_expand = sub.add_parser("expand", help="Walk the call graph around a qname.")
    p_expand.add_argument("qname")
    p_expand.add_argument(
        "--direction", choices=["callees", "callers", "both"], default="callees"
    )
    p_expand.add_argument("--depth", type=int, default=1)
    p_expand.add_argument("--root", default=".")
    _add_vendor_args(p_expand)

    p_outline = sub.add_parser(
        "outline",
        help="Show the symbol tree of a file or every indexed file in a directory.",
    )
    p_outline.add_argument("path")
    p_outline.add_argument(
        "--max-files", dest="max_files", type=int, default=50,
        help="Cap on number of files to outline in directory mode (default 50).",
    )
    p_outline.add_argument(
        "--with-bodies", dest="with_bodies", action="store_true",
        help=(
            "Inline each top-level symbol's source body. Pairs with directory "
            "mode for one-shot 'enumerate every X in this folder' audits."
        ),
    )
    p_outline.add_argument("--root", default=".")
    _add_vendor_args(p_outline)

    p_source = sub.add_parser("source", help="Show the source of a single symbol.")
    p_source.add_argument("qname")
    p_source.add_argument("--with-neighbors", action="store_true")
    p_source.add_argument("--root", default=".")
    _add_vendor_args(p_source)

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
    _add_vendor_args(p_context)

    p_find = sub.add_parser(
        "find",
        help="Exhaustive literal-substring search over indexed symbol bodies.",
    )
    p_find.add_argument("literal")
    p_find.add_argument(
        "--in", dest="in_path", default=None, metavar="PATH",
        help="Restrict the scan to symbols under this path prefix.",
    )
    p_find.add_argument("--kind", default=None)
    p_find.add_argument(
        "--with-bodies", dest="with_bodies", action="store_true",
        help="Inline each match's enclosing-symbol source body.",
    )
    p_find.add_argument(
        "--with-callers", dest="with_callers", action="store_true",
        help="Attach depth-1 callers (deduped) to each match.",
    )
    p_find.add_argument(
        "--max-results", dest="max_results", type=int, default=500,
    )
    p_find.add_argument("--root", default=".")
    _add_vendor_args(p_find)

    p_grep = sub.add_parser(
        "grep",
        help=(
            "Literal or regex search over EVERY text file under the root "
            "(markdown, configs, code, docs). Annotates code-file hits "
            "with the enclosing-symbol qname."
        ),
    )
    p_grep.add_argument("pattern")
    p_grep.add_argument(
        "--regex", action="store_true",
        help="Treat the pattern as a Python regex instead of a literal substring.",
    )
    p_grep.add_argument(
        "-i", "--ignore-case", dest="case_insensitive", action="store_true",
        help="Case-insensitive match.",
    )
    p_grep.add_argument(
        "--in", dest="in_path", default=None, metavar="PATH",
        help="Restrict to files under this path (relative or absolute).",
    )
    p_grep.add_argument(
        "-C", "--context", dest="context_lines", type=int, default=1,
        help="Lines of context before/after each match (default 1, 0 to disable).",
    )
    p_grep.add_argument(
        "--max-results", dest="max_results", type=int, default=200,
    )
    p_grep.add_argument(
        "--max-files", dest="max_files", type=int, default=5000,
        help="Cap on files scanned (early exit on huge trees).",
    )
    p_grep.add_argument(
        "--no-definitions-first", dest="definitions_first",
        action="store_false", default=True,
        help=(
            "Keep matches in natural file/line order. Default is to put "
            "declaration-shaped lines (def/class/function/const/...) before "
            "import/usage lines so 'where is X defined' surfaces fast."
        ),
    )
    p_grep.add_argument("--root", default=".")

    p_map = sub.add_parser(
        "map",
        help=(
            "Repo-wide table of contents — every indexed file's top-level "
            "symbols, grouped by directory."
        ),
    )
    p_map.add_argument(
        "--depth", type=int, default=1, choices=(1, 2),
        help=(
            "1 (default) = top-level symbols only. 2 = also include direct "
            "children (class methods, nested functions)."
        ),
    )
    p_map.add_argument(
        "--prefix", default=None, metavar="PATH",
        help="Restrict the map to files under <root>/<prefix> (e.g. src/).",
    )
    p_map.add_argument(
        "--mode", default="lean", choices=("lean", "full"),
        help=(
            "lean (default): omit per-symbol signatures and line ranges to "
            "keep the orientation payload small. full: include them — call "
            "outline <file> instead when you need that detail."
        ),
    )
    p_map.add_argument("--root", default=".")
    _add_vendor_args(p_map)

    p_vendor = sub.add_parser(
        "vendor",
        help="List or forget on-demand-indexed third-party packages.",
    )
    vendor_sub = p_vendor.add_subparsers(dest="vendor_cmd", required=True)
    p_vendor_list = vendor_sub.add_parser(
        "list", help="Show indexed vendor packages (and what's available to index)."
    )
    p_vendor_list.add_argument("--root", default=".")
    p_vendor_forget = vendor_sub.add_parser(
        "forget", help="Drop a vendor package's symbols from the index."
    )
    p_vendor_forget.add_argument("name")
    p_vendor_forget.add_argument("--root", default=".")

    p_roots = sub.add_parser(
        "roots",
        help="Show which indexed roots snapctx would query from this directory.",
    )
    p_roots.add_argument("root", nargs="?", default=".", help="Start path (default: cwd)")

    p_edit = sub.add_parser(
        "edit",
        help=(
            "Replace a symbol's body by qname. New body is read from a "
            "file (positional) or stdin (--stdin)."
        ),
    )
    p_edit.add_argument("qname", help="Fully qualified symbol name to replace.")
    p_edit.add_argument(
        "body_file", nargs="?", default=None,
        help="Path to a file containing the new body. Mutually exclusive with --stdin.",
    )
    p_edit.add_argument(
        "--stdin", action="store_true",
        help="Read the new body from standard input instead of a file.",
    )
    p_edit.add_argument("--root", default=".")

    p_insert = sub.add_parser(
        "insert",
        help=(
            "Insert a new top-level symbol adjacent to an anchor symbol. "
            "Body read from file or stdin."
        ),
    )
    p_insert.add_argument(
        "anchor_qname",
        help="Existing symbol to anchor against (insert before/after it).",
    )
    p_insert.add_argument(
        "body_file", nargs="?", default=None,
        help="Path to a file containing the new symbol's text.",
    )
    p_insert.add_argument(
        "--stdin", action="store_true",
        help="Read the new text from stdin instead of a file.",
    )
    p_insert.add_argument(
        "--position", choices=("before", "after"), default="after",
        help="Insert before or after the anchor symbol (default: after).",
    )
    p_insert.add_argument("--root", default=".")

    p_delete = sub.add_parser(
        "delete",
        help=(
            "Delete a symbol by qname. Refuses if the file would no "
            "longer parse; trims one surrounding blank line."
        ),
    )
    p_delete.add_argument("qname", help="Fully qualified symbol name to remove.")
    p_delete.add_argument("--root", default=".")

    p_import_add = sub.add_parser(
        "import-add",
        help=(
            "Add an import line to a file. Idempotent. Python: "
            "docstring-aware (lands AFTER a leading module docstring)."
        ),
    )
    p_import_add.add_argument("file", help="File path (relative to root, or absolute).")
    p_import_add.add_argument(
        "statement",
        help="Full import line, e.g. 'from typing import Any' or 'import json'.",
    )
    p_import_add.add_argument("--root", default=".")

    p_import_rm = sub.add_parser(
        "import-remove",
        help="Remove an import line from a file. Idempotent (no-op if absent).",
    )
    p_import_rm.add_argument("file", help="File path (relative to root, or absolute).")
    p_import_rm.add_argument(
        "statement",
        help="Exact import line to remove (matched after stripping whitespace).",
    )
    p_import_rm.add_argument("--root", default=".")

    return parser


# ---------- main ----------


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    global _OUTPUT_STYLE
    _OUTPUT_STYLE = getattr(args, "output_style", "auto")

    # Index / watch: per-root, no fan-out. Indexing creates ``.snapctx``
    # if missing, so no discovery is needed — operate on the explicit path.
    if args.cmd == "index":
        _emit(index_root(args.root, force=args.force))
        return 0

    if args.cmd == "watch":
        run_watch(Path(args.root), debounce_seconds=args.debounce)
        return 0

    if args.cmd == "roots":
        return _print_roots(args.root)

    if args.cmd == "vendor":
        return _vendor_dispatch(args)

    # Query / edit commands: discover, resolve scope, refresh, dispatch.
    roots, anchor = _resolve_roots(args.root)
    if not roots:
        roots = _bootstrap_first_index(anchor)
        if not roots:
            return 2
    else:
        roots = _extend_with_subprojects(anchor, roots)

    # Scope resolution is cheap (~3 ms) so we run it BEFORE the repo
    # auto-refresh: if the query is scoped to a vendor package, the repo's
    # index isn't being queried and SHA-skipping its 300+ files is pure
    # waste (~750 ms on a real project). Vendor packages are
    # built-once-and-forget — no per-call refresh needed.
    write_cmds = ("edit", "insert", "delete", "import-add", "import-remove")
    if args.cmd not in write_cmds:
        _resolve_query_scope(roots, args)
    else:
        # Vendor scope is a read-only concept; write ops refuse it via the API.
        args.scope = None
    if args.scope is None:
        _refresh_indexes(roots)

    if args.cmd == "edit":
        return _edit_dispatch(args, roots, anchor)
    if args.cmd == "insert":
        return _insert_dispatch(args, roots, anchor)
    if args.cmd == "delete":
        return _delete_dispatch(args, roots, anchor)
    if args.cmd == "import-add":
        return _import_add_dispatch(args, roots, anchor)
    if args.cmd == "import-remove":
        return _import_remove_dispatch(args, roots, anchor)

    cmd = _QUERY_BY_NAME.get(args.cmd)
    if cmd is None:
        parser.error(f"unknown command: {args.cmd}")
        return 2

    _emit(cmd.call(args, roots, anchor))
    return 0


def _edit_dispatch(args: argparse.Namespace, roots: list[Path], anchor: Path) -> int:
    """Read new body from file or stdin, dispatch edit_symbol.

    Refresh has already happened upstream so the index reflects the
    current file SHA before the staleness check runs.
    """
    if args.stdin and args.body_file:
        sys.stderr.write("snapctx: pass either body_file or --stdin, not both.\n")
        return 2
    if args.stdin:
        new_body = sys.stdin.read()
    elif args.body_file:
        try:
            new_body = Path(args.body_file).read_text(encoding="utf-8")
        except OSError as e:
            sys.stderr.write(f"snapctx: cannot read body_file: {e}\n")
            return 2
    else:
        sys.stderr.write(
            "snapctx: edit needs a body — pass a file path or --stdin.\n"
        )
        return 2

    if len(roots) > 1:
        result = edit_symbol_multi(args.qname, new_body, roots=roots, anchor=anchor)
    else:
        result = edit_symbol(args.qname, new_body, root=roots[0])
    _emit(result)
    return 0 if "error" not in result else 1


def _insert_dispatch(args: argparse.Namespace, roots: list[Path], anchor: Path) -> int:
    """Read new text from file/stdin, dispatch insert_symbol."""
    if args.stdin and args.body_file:
        sys.stderr.write("snapctx: pass either body_file or --stdin, not both.\n")
        return 2
    if args.stdin:
        new_text = sys.stdin.read()
    elif args.body_file:
        try:
            new_text = Path(args.body_file).read_text(encoding="utf-8")
        except OSError as e:
            sys.stderr.write(f"snapctx: cannot read body_file: {e}\n")
            return 2
    else:
        sys.stderr.write(
            "snapctx: insert needs a body — pass a file path or --stdin.\n"
        )
        return 2

    if len(roots) > 1:
        result = insert_symbol_multi(
            args.anchor_qname, new_text,
            roots=roots, position=args.position, anchor=anchor,
        )
    else:
        result = insert_symbol(
            args.anchor_qname, new_text,
            root=roots[0], position=args.position,
        )
    _emit(result)
    return 0 if "error" not in result else 1


def _delete_dispatch(args: argparse.Namespace, roots: list[Path], anchor: Path) -> int:
    """Dispatch ``delete_symbol`` (single or multi-root by qname)."""
    if len(roots) > 1:
        result = delete_symbol_multi(args.qname, roots=roots, anchor=anchor)
    else:
        result = delete_symbol(args.qname, root=roots[0])
    _emit(result)
    return 0 if "error" not in result else 1


def _import_add_dispatch(
    args: argparse.Namespace, roots: list[Path], anchor: Path,
) -> int:
    if len(roots) > 1:
        result = add_import_multi(
            args.file, args.statement, roots=roots, anchor=anchor,
        )
    else:
        result = add_import(args.file, args.statement, root=roots[0])
    _emit(result)
    return 0 if "error" not in result else 1


def _import_remove_dispatch(
    args: argparse.Namespace, roots: list[Path], anchor: Path,
) -> int:
    if len(roots) > 1:
        result = remove_import_multi(
            args.file, args.statement, roots=roots, anchor=anchor,
        )
    else:
        result = remove_import(args.file, args.statement, root=roots[0])
    _emit(result)
    return 0 if "error" not in result else 1


def _resolve_query_scope(roots: list[Path], args: argparse.Namespace) -> None:
    """Pick the right index for this query: repo's own, or one vendor package's.

    Two routing inputs:
    - ``<pkg>: <rest>`` prefix in the free-text query field (``query`` for
      search/context). The prefix is stripped from the query before
      dispatch so the index sees just the actual question.
    - ``--pkg <name>`` flag (``args.pkg``). Same effect, available on
      every query command for cases without a free-text field.

    With either input, the vendor package is built on demand (no-op when
    already indexed). The resolved scope is attached as ``args.scope``
    for ``QueryCommand.call`` to forward to the api function.

    No prefix and no flag → ``args.scope`` stays ``None`` and the query
    runs against the repo's own index.
    """
    args.scope = None
    explicit = getattr(args, "pkg", None)
    query_text = getattr(args, "query", None) or ""
    root = roots[0]  # vendor scope is single-root only (enforced at dispatch)

    scope: str | None = None
    if explicit:
        scope = explicit
    elif query_text:
        prefix, stripped = parse_query_prefix(query_text, root)
        if prefix is not None:
            scope = prefix
            args.query = stripped

    if scope is None:
        return

    if len(roots) > 1:
        # Defer the multi-root check to dispatch time so the error path is
        # consistent — but record the scope so QueryCommand.call surfaces it.
        args.scope = scope
        return

    ensure_vendor_indexed(root, scope)
    args.scope = scope


def _vendor_dispatch(args: argparse.Namespace) -> int:
    roots, _ = _resolve_roots(args.root)
    if not roots:
        sys.stderr.write(
            f"snapctx: no .snapctx/index.db reachable from {args.root}. "
            f"Run a query or `snapctx index` first.\n"
        )
        return 2
    root = roots[0]

    if args.vendor_cmd == "list":
        indexed = list_indexed_vendors(root)
        available = sorted(discover_packages(root).keys())
        _emit({"root": str(root), "indexed": indexed, "available": available})
        return 0

    if args.vendor_cmd == "forget":
        ok = forget_vendor(root, args.name)
        _emit({"root": str(root), "package": args.name, "removed": ok})
        return 0 if ok else 1

    return 2


def _print_roots(start: str) -> int:
    roots, anchor = _resolve_roots(start)
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
    _emit(out)
    return 0 if roots else 1


if __name__ == "__main__":
    sys.exit(main())
