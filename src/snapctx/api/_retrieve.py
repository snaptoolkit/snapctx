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
    resolve_qname,
    row_to_symbol_dict,
)


def outline(
    path: str | Path,
    root: str | Path = ".",
    scope: str | None = None,
    max_files: int = 50,
    with_bodies: bool = False,
    body_char_cap: int = 1500,
) -> dict:
    """List all symbols defined in a file (or every indexed file in a directory).

    Accepts an absolute path or a path relative to ``root``. Returns the file's
    symbol tree in source order, each node carrying its signature, one-line
    docstring summary, and line range. No bodies by default.

    **Directory mode** — when ``path`` resolves to a directory, every indexed
    file under that directory (recursively) is outlined and returned together
    in one response. This is the exhaustive-enumeration path: grep+read's
    natural strength was directory globbing; ``outline <dir>`` matches it.
    Capped at ``max_files`` files to keep responses bounded; the response sets
    ``truncated: true`` when the cap was hit.

    ``with_bodies=True`` inlines the source body of every top-level symbol
    (capped per-symbol at ``body_char_cap``). For directory-mode audits this
    collapses ``outline <dir>`` + N follow-up ``source <qname>`` calls into a
    single response — the same one-shot pattern ``search --with-bodies``
    delivers, but anchored on directory structure rather than ranking.
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

    if target.is_dir():
        return _outline_directory(
            root_path, target, scope=scope, max_files=max_files,
            with_bodies=with_bodies, body_char_cap=body_char_cap,
        )

    file_str = str(target)
    idx = open_index(root_path, scope=scope)
    try:
        rows = idx.symbols_in_file(file_str)
        # Distinguish "file unknown to index" from "file indexed, zero symbols".
        # Same empty-rows result, but very different remediation. See issue #12.
        is_indexed = idx.current_sha(file_str) is not None
    finally:
        idx.close()

    if not rows:
        if not target.exists():
            hint = (
                f"File {file_str} does not exist. Check the path or, if it "
                "was just created, re-run a snapctx query to refresh the index."
            )
        elif is_indexed:
            hint = (
                f"File exists and is indexed but contains no symbols snapctx "
                "extracts (e.g. type-only modules, top-level constants only, "
                "or a parser-unsupported file type). Use `snapctx_grep` or "
                "read the file directly for raw contents."
            )
        else:
            hint = (
                f"No symbols indexed for {file_str}. Did you run `snapctx "
                "index` on this root?"
            )
        return {"file": file_str, "symbols": [], "hint": hint}

    by_qname = {row["qname"]: row for row in rows}
    tree = _nest_symbols(
        rows, by_qname,
        with_bodies=with_bodies, body_char_cap=body_char_cap,
    )
    response: dict = {"file": file_str, "symbols": tree}
    # Nudge toward the one-call shape when the file is small enough that
    # bodies fit comfortably in one response. Saves the agent from doing
    # outline-then-N-source for files with a handful of symbols.
    if not with_bodies and 0 < len(tree) <= 10:
        response["hint"] = (
            f"Add --with-bodies to get all {len(tree)} top-level "
            "symbols' source in one call (no follow-up `source` needed)."
        )
    return response


def _outline_directory(
    root_path: Path, dir_path: Path, *,
    scope: str | None, max_files: int,
    with_bodies: bool = False, body_char_cap: int = 1500,
) -> dict:
    """Outline every indexed source file under a directory.

    Used for exhaustive enumeration ("list every middleware in
    ``src/middleware/``"). Reads ``files.path`` rows directly from the
    index (cheap; one SQL query) so we don't re-walk the filesystem.
    """
    idx = open_index(root_path, scope=scope)
    try:
        prefix = str(dir_path).rstrip("/") + "/"
        rows = idx.conn.execute(
            "SELECT DISTINCT file FROM symbols "
            "WHERE file LIKE ? || '%' ORDER BY file",
            (prefix,),
        ).fetchall()
        all_files = [r["file"] for r in rows]
        files = all_files[:max_files]

        outlines: list[dict] = []
        for f in files:
            file_rows = idx.symbols_in_file(f)
            if not file_rows:
                continue
            by_qname = {r["qname"]: r for r in file_rows}
            outlines.append({
                "file": f,
                "symbols": _nest_symbols(
                    file_rows, by_qname,
                    with_bodies=with_bodies, body_char_cap=body_char_cap,
                ),
            })
    finally:
        idx.close()

    response: dict = {
        "directory": str(dir_path),
        "files": outlines,
        "file_count": len(outlines),
    }
    if len(all_files) > max_files:
        response["truncated"] = True
        response["total_files"] = len(all_files)
        response["hint"] = (
            f"Showing first {max_files} of {len(all_files)} indexed files. "
            f"Pass max_files=N to widen, or narrow the path."
        )
    if not outlines:
        response["hint"] = (
            f"No indexed files under {dir_path}. Did you run `snapctx index` "
            f"on this root, and is the directory under it?"
        )
    elif not with_bodies and "hint" not in response:
        # Directory enumeration's killer feature is one-call exhaustive
        # audit; surface --with-bodies as the natural next step.
        response["hint"] = (
            f"Got {len(outlines)} files' symbol trees. Add --with-bodies "
            "to inline each top-level symbol's source — "
            "one call answers a 'list every X in this folder' audit."
        )
    return response


def _nest_symbols(
    rows: list[sqlite3.Row],
    by_qname: dict[str, sqlite3.Row],
    *,
    with_bodies: bool = False,
    body_char_cap: int = 1500,
) -> list[dict]:
    """Build a tree from a flat list of Symbols ordered by line_start.

    A symbol's children are the symbols whose parent_qname is this symbol's qname.
    When ``with_bodies`` is set, each *root* symbol gets its source body
    inlined (root only — children are within the root's body anyway).
    """
    children_of: dict[str | None, list[sqlite3.Row]] = {}
    for row in rows:
        children_of.setdefault(row["parent_qname"], []).append(row)

    def build(row: sqlite3.Row, *, attach_body: bool) -> dict:
        d = row_to_symbol_dict(row)
        d["docstring"] = docstring_summary(row["docstring"])
        if attach_body:
            body = _read_symbol_body(row, body_char_cap)
            if body is not None:
                d["source"] = body
        kids = children_of.get(row["qname"], [])
        if kids:
            d["children"] = [build(k, attach_body=False) for k in kids]
        return d

    # Roots are rows whose parent_qname is None, or whose parent isn't in this file.
    roots = [r for r in rows if r["parent_qname"] is None or r["parent_qname"] not in by_qname]
    return [build(r, attach_body=with_bodies) for r in roots]


def _read_symbol_body(row, body_char_cap: int) -> str | None:
    """Slice a symbol's source body from its file, capped at ``body_char_cap``.

    Returns ``None`` on an unreadable file so the response stays JSON-clean.
    """
    try:
        text = Path(row["file"]).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    lines = text.splitlines()
    start = max(1, int(row["line_start"]))
    end = max(start, int(row["line_end"]))
    body = "\n".join(lines[start - 1 : end])
    if len(body) > body_char_cap:
        body = body[:body_char_cap] + (
            f"\n# ... truncated ({len(body) - body_char_cap} chars) ..."
        )
    return body


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
        # Resolve the qname forgivingly: if the literal qname doesn't
        # exist, try common LLM paraphrases (stale ``.tsx``/``.py`` on
        # the module, dotted-vs-slashed path style). When a paraphrase
        # matches we surface a hint so the caller learns the canonical
        # form for next time.
        canonical, paraphrase_hint = resolve_qname(idx, qname)
        if canonical is None:
            return {
                "qname": qname,
                "error": "not_found",
                "hint": f"No symbol {qname!r} in index.",
            }
        row = idx.get_symbol(canonical)

        path = Path(row["file"])
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as e:
            return {"qname": qname, "error": f"read_failed: {e}"}

        body = "\n".join(lines[row["line_start"] - 1 : row["line_end"]])

        result = {
            "qname": canonical,
            "signature": row["signature"],
            "file": row["file"],
            "lines": f"{row['line_start']}-{row['line_end']}",
            "source": body,
        }
        if paraphrase_hint is not None:
            result["paraphrase_hint"] = (
                f"Resolved {qname!r} → {canonical!r} ({paraphrase_hint}). "
                f"Use {canonical!r} verbatim in subsequent calls."
            )

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
