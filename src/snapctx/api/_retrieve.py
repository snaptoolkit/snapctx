"""Symbol-level retrieval: ``outline`` (file → tree) and ``get_source`` (qname → body).

Both are thin readers over the index. ``outline`` builds a parent-nested
tree so a UI can show the file's structure at a glance; ``get_source``
slices the file at the symbol's stored line range and optionally appends
its resolved callees.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from snapctx.api._common import (
    docstring_summary,
    open_index,
    row_to_symbol_dict,
)


def outline(path: str | Path, root: str | Path = ".", scope: str | None = None) -> dict:
    """List all symbols defined in a file, nested by parent.

    Accepts an absolute path or a path relative to ``root``. Returns the file's
    symbol tree in source order, each node carrying its signature, one-line
    docstring summary, and line range. No bodies.

    Use this instead of reading a whole file when you only need to know what
    it defines — typically a 10x token savings over ``get_source`` of the file.
    """
    root_path = Path(root).resolve()
    target = Path(path)
    if not target.is_absolute():
        # In a vendor scope, relative paths are relative to the package's
        # own root (the package dir), not the repo root, since that's how
        # files are anchored in the per-package index.
        from snapctx.vendor import vendor_index_dir
        if scope is not None:
            from snapctx.vendor import discover_packages
            pkg = discover_packages(root_path).get(scope)
            anchor = pkg or vendor_index_dir(root_path, scope).parent
            target = (anchor / target).resolve()
        else:
            target = (root_path / target).resolve()
    file_str = str(target)

    idx = open_index(root_path, scope=scope)
    try:
        rows = idx.symbols_in_file(file_str)
    finally:
        idx.close()

    if not rows:
        return {
            "file": file_str,
            "symbols": [],
            "hint": f"No symbols indexed for {file_str}. Did you run `snapctx index` on this root?",
        }

    by_qname = {row["qname"]: row for row in rows}
    return {"file": file_str, "symbols": _nest_symbols(rows, by_qname)}


def _nest_symbols(rows: list[sqlite3.Row], by_qname: dict[str, sqlite3.Row]) -> list[dict]:
    """Build a tree from a flat list of Symbols ordered by line_start.

    A symbol's children are the symbols whose parent_qname is this symbol's qname.
    """
    children_of: dict[str | None, list[sqlite3.Row]] = {}
    for row in rows:
        children_of.setdefault(row["parent_qname"], []).append(row)

    def build(row: sqlite3.Row) -> dict:
        d = row_to_symbol_dict(row)
        d["docstring"] = docstring_summary(row["docstring"])
        kids = children_of.get(row["qname"], [])
        if kids:
            d["children"] = [build(k) for k in kids]
        return d

    # Roots are rows whose parent_qname is None, or whose parent isn't in this file.
    roots = [r for r in rows if r["parent_qname"] is None or r["parent_qname"] not in by_qname]
    return [build(r) for r in roots]


def get_source(
    qname: str,
    with_neighbors: bool = False,
    root: str | Path = ".",
    scope: str | None = None,
) -> dict:
    """Return the full source of a symbol, and optionally the signatures of what it calls.

    ``with_neighbors=True`` appends a compact list of this symbol's resolved
    callees (signature + docstring summary only), so the caller can reason
    about the dependency context without a follow-up round-trip.
    """
    root_path = Path(root).resolve()
    idx = open_index(root_path, scope=scope)
    try:
        row = idx.get_symbol(qname)
        if row is None:
            return {
                "qname": qname,
                "error": "not_found",
                "hint": f"No symbol {qname!r} in index.",
            }

        path = Path(row["file"])
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as e:
            return {"qname": qname, "error": f"read_failed: {e}"}

        body = "\n".join(lines[row["line_start"] - 1 : row["line_end"]])

        result = {
            "qname": qname,
            "signature": row["signature"],
            "file": row["file"],
            "lines": f"{row['line_start']}-{row['line_end']}",
            "source": body,
        }

        if with_neighbors:
            callees = []
            for call_row in idx.callees_of(qname):
                if not call_row["callee_qname"]:
                    continue
                neigh = idx.get_symbol(call_row["callee_qname"])
                if neigh is None:
                    continue
                callees.append({
                    "qname": neigh["qname"],
                    "signature": neigh["signature"],
                    "docstring": docstring_summary(neigh["docstring"]),
                })
            result["callees"] = callees

        return result
    finally:
        idx.close()
