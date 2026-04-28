"""Batch symbol edit: apply N edits to a repo in one call.

The single-edit path (``edit_symbol``) costs one LLM turn per symbol
plus one SHA-keyed re-index. For an N-symbol refactor that's N round
trips through the model and N re-indexes — fine for one or two edits,
expensive past that.

``edit_symbol_batch`` collapses the N edits into one tool call:

* All qnames resolved against the index up front.
* Edits grouped by file, sorted by line_start descending so the
  in-memory splice is unambiguous (lower edits don't shift the line
  numbers of higher ones).
* Per-file atomicity: build the candidate file text with every edit
  for that file applied, run the syntax pre-flight once on the
  finished result, write only if it parses. A bad edit on file A
  doesn't cancel file B's edits — each file is independent.
* One ``index_root`` at the end, not N.

Cross-edit conflicts on the same file are surfaced as errors:

* ``duplicate_qname`` — two edits target the same symbol.
* ``overlapping_edits`` — two edits cover overlapping line ranges
  (rare; would require nested-symbol edits).
"""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path

from snapctx.api._common import open_index, resolve_qname
from snapctx.index import sha_bytes

_PYTHON_SUFFIXES = (".py", ".pyi")
_TS_SUFFIXES = (".ts", ".tsx", ".mts", ".cts", ".jsx", ".js", ".mjs", ".cjs")


def edit_symbol_batch(
    edits: list[dict],
    root: str | Path = ".",
    scope: str | None = None,
) -> dict:
    """Apply many symbol edits in one call.

    ``edits`` is a list of ``{"qname": str, "new_body": str}`` dicts.
    Returns ``{"applied": [...], "errors": [...], "files_touched":
    int, "reindex": {...}}``. Per-edit ``applied`` entries mirror the
    single-edit shape (``qname``, ``file``, ``lines_before``,
    ``lines_after``); per-edit ``errors`` carry the same structured
    error fields as ``edit_symbol`` plus the offending qname.
    """
    if scope is not None:
        return {
            "error": "scope_unsupported",
            "hint": "edit_symbol_batch does not support vendor scopes.",
        }
    if not edits:
        return {"applied": [], "errors": [], "files_touched": 0}

    root_path = Path(root).resolve()
    idx = open_index(root_path, scope=None)
    applied: list[dict] = []
    errors: list[dict] = []

    # Phase 1: resolve every qname → (canonical, file, line_start, line_end).
    # Edits whose qname doesn't resolve are recorded as errors immediately
    # but don't block the rest.
    by_file: dict[str, list[dict]] = defaultdict(list)
    try:
        for edit in edits:
            qname = edit.get("qname")
            new_body = edit.get("new_body")
            if not qname or new_body is None:
                errors.append({
                    "qname": qname,
                    "error": "invalid_edit",
                    "hint": "each edit needs 'qname' and 'new_body' keys.",
                })
                continue
            canonical, paraphrase_hint = resolve_qname(idx, qname)
            if canonical is None:
                errors.append({
                    "qname": qname,
                    "error": "not_found",
                    "hint": f"No symbol {qname!r} in index.",
                })
                continue
            row = idx.get_symbol(canonical)
            by_file[row["file"]].append({
                "qname": canonical,
                "original_qname": qname,
                "paraphrase_hint": paraphrase_hint,
                "new_body": new_body,
                "line_start": row["line_start"],
                "line_end": row["line_end"],
            })
    finally:
        # We re-open below; close now so the index isn't held during
        # any I/O on the source files.
        idx.close()

    # Phase 2: per-file atomic apply.
    for file_str, file_edits in by_file.items():
        path = Path(file_str)
        file_outcome = _apply_to_one_file(path, file_edits, root_path)
        if file_outcome["applied"]:
            applied.extend(file_outcome["applied"])
        if file_outcome["errors"]:
            errors.extend(file_outcome["errors"])

    files_touched = sum(
        1 for f in by_file
        if any(a["file"] == f for a in applied)
    )

    # Phase 3: single re-index for the whole batch.
    if files_touched > 0:
        from snapctx.api._indexer import index_root
        refresh = index_root(root_path)
        reindex = {
            "files_updated": refresh["files_updated"],
            "files_removed": refresh["files_removed"],
        }
    else:
        reindex = {"files_updated": 0, "files_removed": 0}

    return {
        "applied": applied,
        "errors": errors,
        "files_touched": files_touched,
        "reindex": reindex,
    }


def _apply_to_one_file(
    path: Path, edits: list[dict], root_path: Path,
) -> dict:
    """Apply all edits targeting one file; per-file atomicity.

    Either every edit lands and the file is rewritten once, or no
    edit lands and each is reported in ``errors``. Cross-file batches
    are independent — a failure here doesn't affect other files.
    """
    applied: list[dict] = []
    errors: list[dict] = []

    # Detect conflicts up front so we don't waste I/O on a doomed file.
    seen_qnames: set[str] = set()
    for e in edits:
        if e["qname"] in seen_qnames:
            errors.append({
                "qname": e["original_qname"],
                "error": "duplicate_qname",
                "hint": f"Multiple edits target {e['qname']!r} in this batch.",
            })
            return {"applied": [], "errors": errors}
        seen_qnames.add(e["qname"])

    # Sort by line_start ascending to detect overlap, then descending
    # for splice (apply bottom-up so earlier lines stay valid).
    by_lines = sorted(edits, key=lambda e: e["line_start"])
    for prev, curr in zip(by_lines, by_lines[1:]):
        if curr["line_start"] <= prev["line_end"]:
            errors.append({
                "qname": curr["original_qname"],
                "error": "overlapping_edits",
                "hint": (
                    f"Edit on {curr['qname']!r} (lines {curr['line_start']}-"
                    f"{curr['line_end']}) overlaps the prior edit on "
                    f"{prev['qname']!r} (lines {prev['line_start']}-"
                    f"{prev['line_end']})."
                ),
            })
            return {"applied": [], "errors": errors}

    # Read file once.
    try:
        data = path.read_bytes()
    except OSError as e:
        return {
            "applied": [],
            "errors": [{
                "qname": edits[0]["original_qname"],
                "error": "read_failed",
                "hint": f"{path}: {e}",
            }],
        }

    # Staleness check via the same SHA the index recorded. We open the
    # index briefly here so we don't hold it across the whole batch.
    idx = open_index(root_path, scope=None)
    try:
        indexed_sha = idx.current_sha(str(path))
    finally:
        idx.close()
    if indexed_sha != sha_bytes(data):
        return {
            "applied": [],
            "errors": [{
                "qname": edits[0]["original_qname"],
                "error": "stale_coordinates",
                "hint": (
                    f"File {str(path)!r} has changed since indexing. "
                    "Re-query and retry."
                ),
            }],
        }

    text = data.decode("utf-8", errors="replace")
    had_trailing_nl = text.endswith("\n")
    lines = text.split("\n")
    if had_trailing_nl:
        lines.pop()

    # Apply bottom-up so unaffected line numbers stay valid.
    for e in sorted(edits, key=lambda x: x["line_start"], reverse=True):
        ls, le = e["line_start"], e["line_end"]
        if ls < 1 or le > len(lines) or ls > le:
            return {
                "applied": [],
                "errors": [{
                    "qname": e["original_qname"],
                    "error": "stale_coordinates",
                    "hint": (
                        f"Stored line range {ls}-{le} for {e['qname']!r} "
                        f"is outside the file (now {len(lines)} lines)."
                    ),
                }],
            }
        body_lines = e["new_body"].split("\n")
        if body_lines and body_lines[-1] == "":
            body_lines.pop()
        lines = lines[: ls - 1] + body_lines + lines[le:]

    new_text = "\n".join(lines)
    if had_trailing_nl:
        new_text += "\n"

    # Single syntax check on the finished candidate file.
    if path.suffix in _PYTHON_SUFFIXES:
        try:
            ast.parse(new_text)
        except SyntaxError as syn:
            return {
                "applied": [],
                "errors": [{
                    "qname": "<batch>",
                    "file": str(path),
                    "error": "syntax_error",
                    "hint": (
                        f"Combined batch edit on {path.name!r} won't parse: "
                        f"{syn.msg} at line {syn.lineno}, col {syn.offset}. "
                        "No edits in this file were applied."
                    ),
                }],
            }
    elif path.suffix in _TS_SUFFIXES:
        from snapctx.parsers.typescript import find_syntax_error
        err = find_syntax_error(new_text, path.suffix)
        if err is not None:
            line, col = err
            return {
                "applied": [],
                "errors": [{
                    "qname": "<batch>",
                    "file": str(path),
                    "error": "syntax_error",
                    "hint": (
                        f"Combined batch edit on {path.name!r} won't parse "
                        f"(tree-sitter error at line {line}, col {col}). "
                        "No edits in this file were applied."
                    ),
                }],
            }

    try:
        path.write_text(new_text, encoding="utf-8")
    except OSError as e:
        return {
            "applied": [],
            "errors": [{
                "qname": edits[0]["original_qname"],
                "error": "write_failed",
                "hint": f"{path}: {e}",
            }],
        }

    # Build per-edit applied entries. We can't easily report
    # post-edit line ranges in cascade-shifted batches; report the
    # original line range and how many lines the new body has.
    for e in edits:
        body_line_count = e["new_body"].count("\n")
        if not e["new_body"].endswith("\n"):
            body_line_count += 1
        entry = {
            "qname": e["qname"],
            "file": str(path),
            "lines_before": f"{e['line_start']}-{e['line_end']}",
            "lines_replaced": e["line_end"] - e["line_start"] + 1,
            "lines_written": body_line_count,
        }
        if e["paraphrase_hint"] is not None:
            entry["paraphrase_hint"] = (
                f"Resolved {e['original_qname']!r} → {e['qname']!r} "
                f"({e['paraphrase_hint']})."
            )
        applied.append(entry)
    return {"applied": applied, "errors": []}
