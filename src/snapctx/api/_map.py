"""Repo-wide table of contents — ``map`` (top-level symbols, grouped by directory).

A query-free starting point for an agent that has just landed in a repo
and is asking "what is this thing?". Pulls every top-level symbol's
signature and one-line docstring summary out of the index in a single
SQL pass, groups them by directory, and returns the result in source-
tree order.

Complements ``outline`` (one file → tree) and ``context`` (query →
focused pack): ``map`` is what you read first when you don't yet have
a question.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from pathlib import Path

from snapctx.api._common import (
    docstring_summary,
    open_index,
    rough_token_count,
)


def map_repo(
    root: str | Path = ".",
    scope: str | None = None,
    *,
    depth: int = 1,
    prefix: str | None = None,
) -> dict:
    """Return a directory-grouped tree of every indexed file's top-level symbols.

    Each symbol carries its qname (for follow-up ``source <qname>`` calls),
    kind, signature, one-line docstring summary, and source line range.
    No bodies — this is a survey, not a deep read.

    ``depth=1`` (default) lists only top-level symbols (``parent_qname
    IS NULL``). ``depth=2`` adds direct children — class methods, nested
    functions — useful on class-heavy code where the top-level alone is
    too sparse. Higher depths aren't supported: at that point use
    ``outline <file>`` on a specific file.

    ``prefix`` filters to files under ``<root>/<prefix>`` (e.g.
    ``prefix='src/'``), for scoping a map to a sub-tree.
    """
    if depth not in (1, 2):
        raise ValueError(f"depth must be 1 or 2, got {depth}")

    root_path = Path(root).resolve()
    idx = open_index(root_path, scope=scope)
    try:
        if prefix:
            like_root = (root_path / prefix.lstrip("/")).resolve()
        else:
            like_root = root_path
        like = str(like_root).rstrip("/") + "/%"

        # depth=1 narrows to top-level rows in SQL (cheap, hits the
        # parent_qname index). depth=2 fetches everything for the
        # filtered files and prunes in Python — the row count is
        # already small (5k-ish on a mid-size repo), so a second
        # SQL filter step isn't worth the complexity.
        if depth == 1:
            sql = (
                "SELECT * FROM symbols "
                "WHERE file LIKE ? AND parent_qname IS NULL "
                "ORDER BY file ASC, line_start ASC"
            )
        else:
            sql = (
                "SELECT * FROM symbols "
                "WHERE file LIKE ? "
                "ORDER BY file ASC, line_start ASC"
            )
        rows = idx.conn.execute(sql, (like,)).fetchall()
    finally:
        idx.close()

    by_file: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        by_file[row["file"]].append(row)

    files_payload: list[dict] = []
    symbol_count = 0
    for file, file_rows in by_file.items():
        # Hoist the file's module docstring up to a file-level summary
        # and drop the synthetic ``module:`` symbol — its payload is the
        # file's purpose, which structurally belongs to the file, not
        # to a thing inside it. Saves ~150 chars per file.
        file_summary: str | None = None
        non_module_rows: list[sqlite3.Row] = []
        for r in file_rows:
            if r["kind"] == "module" and r["parent_qname"] is None:
                file_summary = file_summary or docstring_summary(r["docstring"])
                continue
            non_module_rows.append(r)

        symbols = _build_file_symbols(non_module_rows, depth)
        if not symbols and not file_summary:
            continue

        entry: dict = {"file": _relative(file, root_path)}
        if file_summary:
            entry["summary"] = file_summary
        entry["symbols"] = symbols
        files_payload.append(entry)
        symbol_count += sum(1 + len(s.get("children", [])) for s in symbols)

    by_dir: dict[str, list[dict]] = defaultdict(list)
    for f in files_payload:
        d = str(Path(f["file"]).parent)
        by_dir[d].append(f)

    directories = [
        {"dir": d, "files": files}
        for d, files in sorted(by_dir.items())
    ]

    payload: dict = {
        "root": str(root_path),
        "depth": depth,
        "directories": directories,
        "file_count": len(files_payload),
        "symbol_count": symbol_count,
    }
    if prefix:
        payload["prefix"] = prefix
    if not directories:
        scope_hint = f" with prefix={prefix!r}" if prefix else ""
        payload["hint"] = (
            f"No indexed symbols under {root_path}{scope_hint}. "
            "Did you run `snapctx index` here?"
        )
    payload["token_estimate"] = rough_token_count(payload)
    return payload


def _build_file_symbols(rows: list[sqlite3.Row], depth: int) -> list[dict]:
    """Group a file's rows into the depth-bounded tree shape.

    For ``depth=1``, every row IS a top-level symbol (the SQL filtered
    to ``parent_qname IS NULL``). For ``depth=2``, fold direct children
    under their parent and drop any deeper rows.
    """
    if depth == 1:
        return [_format_symbol(r) for r in rows]

    by_qname = {r["qname"]: r for r in rows}
    children_of: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        parent = r["parent_qname"]
        if parent is not None and parent in by_qname:
            children_of[parent].append(r)

    out: list[dict] = []
    for r in rows:
        # Top-level: parent is None, or parent isn't in this file (e.g.
        # cross-file inheritance — same edge case ``outline`` handles).
        if r["parent_qname"] is None or r["parent_qname"] not in by_qname:
            d = _format_symbol(r)
            kids = children_of.get(r["qname"], [])
            if kids:
                d["children"] = [_format_symbol(k) for k in kids]
            out.append(d)
    return out


def _format_symbol(row: sqlite3.Row) -> dict:
    out: dict = {
        "qname": row["qname"],
        "kind": row["kind"],
        "signature": row["signature"],
        "docstring": docstring_summary(row["docstring"]),
        "lines": f"{row['line_start']}-{row['line_end']}",
    }
    # Decorators are stored separately from the signature, so without
    # this a navigator misses the most identifying fact about a symbol
    # (``@app.route('/login')``, ``@dataclass(frozen=True)``,
    # ``@pytest.fixture``). Cheap to include — usually 0 or 1 per
    # symbol, never more than a handful.
    if row["decorators"]:
        out["decorators"] = row["decorators"].split("\n")
    return out


def _relative(file: str, root: Path) -> str:
    """Return ``file`` relative to ``root`` so payloads stay compact.

    Falls back to the absolute path on the rare cross-root file (e.g.
    a symlinked vendor dir) so callers can still resolve it.
    """
    try:
        return str(Path(file).resolve().relative_to(root))
    except ValueError:
        return file
