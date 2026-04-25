"""Shared helpers used across the api package.

These are the low-level bits every higher-level operation needs:
opening the index, formatting a row, summarizing a docstring, parsing
a "L1-L2" line range, and approximating token counts. Kept in one
place so each operation module stays focused on its actual logic.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from snapctx.index import Index, db_path_for


def open_index(root: Path) -> Index:
    """Open the index for ``root``, raising a friendly error if missing."""
    db = db_path_for(root)
    if not db.exists():
        raise FileNotFoundError(
            f"No index at {db}. Run `snapctx index {root}` first."
        )
    return Index(db)


def row_to_symbol_dict(row: sqlite3.Row, *, include_body_line_range: bool = True) -> dict:
    d = {
        "qname": row["qname"],
        "kind": row["kind"],
        "language": row["language"],
        "signature": row["signature"],
        "docstring": row["docstring"],
        "file": row["file"],
        "parent_qname": row["parent_qname"],
    }
    if include_body_line_range:
        d["lines"] = f"{row['line_start']}-{row['line_end']}"
    if row["decorators"]:
        d["decorators"] = row["decorators"].split("\n")
    return d


def docstring_summary(docstring: str | None) -> str | None:
    """Return just the first sentence/line of a docstring — sized for search results."""
    if not docstring:
        return None
    return docstring.strip().splitlines()[0]


def parse_line_range(lines: str) -> tuple[int, int]:
    if "-" in lines:
        a, b = lines.split("-", 1)
        return int(a), int(b)
    n = int(lines)
    return n, n


def rough_token_count(payload: dict) -> int:
    """Approximate token count as chars/4 over the payload's JSON rendering."""
    return len(json.dumps(payload)) // 4
