"""``find`` — exhaustive literal-substring search over indexed symbol bodies.

Closes the structural gap that ranking-based search has against grep on
audit-class questions. ``search`` returns ranked top-K (which by design
cuts off the long tail); ``find`` enumerates *every* indexed symbol
whose source body contains the literal — same exhaustiveness as
``grep -F`` plus the index's symbol-awareness so the caller gets back
``(file, line, qname, signature)`` per hit instead of raw lines.

Use cases:
- "Audit every place that uses ``transaction.atomic``" — every site,
  not the top-30 by rank.
- "Find every call to a deprecated API" — exhaustive replacement
  candidates.
- "Where does this string literal appear" — `grep -F` semantics.

Implementation: read each candidate file once into a cache, walk the
indexed symbols, slice each symbol's body from the cache, and check
for the literal as a plain substring (no regex, no tokenization). The
match is per-symbol — when multiple symbols (a class and its method)
both contain the literal, we return the innermost one (by line range)
to match grep's per-line intuition without spamming duplicates.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from snapctx.api._common import (
    docstring_summary,
    open_index,
    row_to_symbol_dict,
)


def find_literal(
    literal: str,
    root: str | Path = ".",
    scope: str | None = None,
    *,
    in_path: str | None = None,
    kind: str | None = None,
    with_bodies: bool = False,
    with_callers: bool = False,
    body_char_cap: int = 1500,
    max_results: int = 500,
) -> dict:
    """Return every indexed symbol whose source body contains ``literal``.

    ``in_path`` narrows the scan to symbols whose ``file`` starts with the
    given prefix (relative or absolute) — useful for "every X under
    src/auth/" without scanning the rest of the repo. ``kind`` filters
    by symbol kind (function, method, class, …) the same way ``search``
    does. ``with_bodies`` inlines each match's source body capped at
    ``body_char_cap`` chars per match. ``with_callers`` attaches the
    depth-1 caller list (deduped by qname) to each hit so audits that
    need impact analysis ("every X site AND who triggers them") fit
    in one call.

    Returns ``{literal, matches, match_count, truncated, hint}``. The
    match list is in source order (by file, then line_start) so an
    audit reads naturally top-to-bottom through each file.
    """
    if not literal:
        return {
            "literal": literal,
            "matches": [],
            "match_count": 0,
            "truncated": False,
            "hint": "Pass a non-empty literal to search for.",
        }

    root_path = Path(root).resolve()
    idx = open_index(root_path, scope=scope)
    try:
        rows = _candidate_rows(idx, in_path=in_path, kind=kind)
        matches = _scan_for_literal(
            rows, literal, with_bodies=with_bodies,
            body_char_cap=body_char_cap, max_results=max_results,
        )
        if with_callers:
            _attach_callers(idx, matches)
    finally:
        idx.close()

    truncated = len(matches) >= max_results
    return {
        "literal": literal,
        "matches": matches,
        "match_count": len(matches),
        "truncated": truncated,
        "hint": _hint_for(
            matches, truncated, max_results, with_bodies,
            with_callers=with_callers,
        ),
    }


def _attach_callers(idx, matches: list[dict]) -> None:
    """Attach a deduped depth-1 caller list to each match in-place.

    A symbol called from N sites has N rows in the calls table; we want the
    distinct caller qnames (with the first call line as the example), since
    "who calls this" usually wants the unique callers list, not every line.
    """
    for m in matches:
        seen: dict[str, int] = {}
        for c in idx.callers_of(m["qname"]):
            cq = c["caller_qname"]
            if cq and cq not in seen:
                seen[cq] = c["line"]
        if seen:
            m["callers"] = [
                {"qname": cq, "line": line} for cq, line in seen.items()
            ]


def _candidate_rows(
    idx, *, in_path: str | None, kind: str | None,
) -> list[sqlite3.Row]:
    """Pull every symbol row that could contain a match, in source order."""
    where: list[str] = []
    args: list = []
    if in_path:
        prefix = str(Path(in_path).resolve()) if Path(in_path).exists() else in_path
        where.append("file LIKE ? || '%'")
        args.append(prefix)
    if kind:
        where.append("kind = ?")
        args.append(kind)
    sql = "SELECT * FROM symbols"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY file ASC, line_start ASC"
    return idx.conn.execute(sql, args).fetchall()


def _scan_for_literal(
    rows: list[sqlite3.Row],
    literal: str,
    *,
    with_bodies: bool,
    body_char_cap: int,
    max_results: int,
) -> list[dict]:
    """Per-file body slice + substring check, with innermost-symbol dedupe.

    When a class body contains a method body that contains the literal,
    both rows would naively match — we want the method (the innermost
    enclosing symbol) so the audit doesn't list class+method redundantly
    for the same line. We do this in two passes: first collect every
    matching (file, line, row) tuple, then per (file, line) keep the
    smallest line-range row.
    """
    file_cache: dict[str, list[str]] = {}
    by_file: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        by_file.setdefault(row["file"], []).append(row)

    # Step 1: identify the lines (per file) where the literal appears,
    # so we don't re-scan the same body for nested symbols.
    file_match_lines: dict[str, list[int]] = {}
    for f in by_file:
        try:
            text = Path(f).read_text(encoding="utf-8", errors="replace")
        except OSError:
            file_cache[f] = []
            continue
        lines = text.splitlines()
        file_cache[f] = lines
        hits: list[int] = []
        for i, line in enumerate(lines, start=1):
            if literal in line:
                hits.append(i)
        if hits:
            file_match_lines[f] = hits

    # Step 2: for each match line, pick the innermost enclosing symbol.
    seen_keys: set[tuple[str, int]] = set()
    out: list[dict] = []
    for f, line_nos in file_match_lines.items():
        rows_in_file = by_file[f]
        for line_no in line_nos:
            enclosing = _innermost(rows_in_file, line_no)
            if enclosing is None:
                continue
            key = (enclosing["qname"], line_no)
            if key in seen_keys:
                continue
            seen_keys.add(key)

            d = row_to_symbol_dict(enclosing)
            d["docstring"] = docstring_summary(enclosing["docstring"])
            d["match_line"] = line_no
            d["match_text"] = file_cache[f][line_no - 1].rstrip()
            if with_bodies:
                start = max(1, int(enclosing["line_start"]))
                end = max(start, int(enclosing["line_end"]))
                body = "\n".join(file_cache[f][start - 1 : end])
                if len(body) > body_char_cap:
                    body = body[:body_char_cap] + (
                        f"\n# ... truncated ({len(body) - body_char_cap} chars) ..."
                    )
                d["source"] = body
            out.append(d)
            if len(out) >= max_results:
                return out
    return out


def _innermost(rows: list[sqlite3.Row], line_no: int) -> sqlite3.Row | None:
    """Smallest-range row whose [line_start, line_end] contains ``line_no``.

    A method nested in a class beats the class because its range is
    tighter. Modules (whole-file ranges) lose to anything specific —
    including a single function whose range happens to span the whole
    file (``def f(): pass`` in a one-line module): without an explicit
    tie-break, ``min`` would return whichever row hit first in
    iteration order. We prefer non-module symbols whenever any are
    present, falling back to the module only when nothing else
    contains the line.
    """
    candidates = [
        r for r in rows
        if r["line_start"] <= line_no <= r["line_end"]
    ]
    if not candidates:
        return None
    non_module = [r for r in candidates if r["kind"] != "module"]
    pool = non_module if non_module else candidates
    return min(pool, key=lambda r: r["line_end"] - r["line_start"])


def _hint_for(
    matches: list[dict], truncated: bool, max_results: int, with_bodies: bool,
    *, with_callers: bool = False,
) -> str:
    if not matches:
        return (
            "No symbol body contains that literal. Note: this scans "
            "indexed source files only — strings inside comments are "
            "indexed but match-position fidelity may differ from grep."
        )
    if truncated:
        return (
            f"Hit max_results cap ({max_results}). Pass --in <path-prefix> "
            "to narrow, or --max-results N to widen."
        )
    if not with_bodies:
        extras = "" if with_callers else (
            " Add --with-callers for impact analysis "
            "(who triggers each site)."
        )
        return (
            f"{len(matches)} sites found. Add --with-bodies to inline each "
            "enclosing symbol's source so you don't need follow-up calls."
            + extras
        )
    suffix = " Callers attached." if with_callers else ""
    return f"{len(matches)} sites found, bodies inlined.{suffix}"
