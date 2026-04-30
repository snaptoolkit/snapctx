"""Token-efficient symbol edit: search/replace within a symbol body.

``edit_symbol`` requires the LLM to emit the COMPLETE new body. For a
50-line function where one line changes, that's 50× the necessary
output tokens — and output tokens are 5× the cost of input on most
provider rate cards.

``edit_symbol_search_replace`` collapses that to the minimum: emit the
exact substring that needs to change (``search``) and what it should
become (``replace``). The match is done against the symbol's body
text (not the whole file) to keep ``search`` as short as possible —
the model only needs enough context to be unique within the symbol,
not the whole file.

Contract:

* ``search`` must occur EXACTLY ONCE in the symbol body (after a
  trailing-whitespace-tolerant comparison). Zero matches → ``not_found``;
  multiple matches → ``ambiguous``. The agent should widen the search
  string until it's unique.
* The replacement reuses the exact same line range as the original
  symbol — we splice the modified body back into the file via the
  same path ``edit_symbol`` already uses (syntax pre-flight, SHA
  staleness recovery, single re-index).
* Newlines in ``search`` and ``replace`` use ``\\n`` — same convention
  as the file on disk.

This is intentionally NOT a fuzzy-match. If ``search`` doesn't appear
verbatim, we refuse and tell the agent. Fuzzy matching against an
LLM-emitted string is the source of an entire category of "phantom
edit" bugs — the model thinks the file looks one way, the file
actually looks another, and a fuzzy match papers over the
disagreement instead of surfacing it.

Same per-file atomicity as ``edit_symbol_batch``: the batch variant
groups all edits to one file, applies them all in memory, runs the
syntax pre-flight once, writes once.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path

from snapctx.api._common import open_index, refresh_file_in_index, resolve_qname
from snapctx.index import sha_bytes
from snapctx.qname import validate_writable_qname

_PYTHON_SUFFIXES = (".py", ".pyi")
_TS_SUFFIXES = (".ts", ".tsx", ".mts", ".cts", ".jsx", ".js", ".mjs", ".cjs")


def edit_symbol_search_replace(
    qname: str,
    search: str,
    replace: str,
    root: str | Path = ".",
    scope: str | None = None,
) -> dict:
    """Replace the unique substring ``search`` with ``replace`` inside ``qname``'s body.

    Returns the same shape as ``edit_symbol`` on success
    (``qname``, ``file``, ``lines_before``, ``lines_after``,
    ``lines_replaced``, ``lines_written``, ``reindex``) plus a
    ``chars_replaced`` count, or ``{"qname", "error", "hint"}`` on
    failure.

    Failure modes (in addition to the ones ``edit_symbol`` returns):

    * ``not_found`` (search) — ``search`` doesn't appear in the symbol
      body. The hint suggests calling ``get_source`` to re-read the
      current body.
    * ``ambiguous`` — ``search`` appears more than once in the symbol
      body. The hint suggests widening the search to make it unique.
    * ``no_change`` — ``search == replace``; nothing to do. Treated as
      an error so the agent learns not to emit no-ops.
    """
    validate_writable_qname(qname)
    if scope is not None:
        return {
            "qname": qname,
            "error": "scope_unsupported",
            "hint": (
                "edit_symbol_search_replace does not support vendor "
                "scopes — vendored packages are read-only."
            ),
        }
    if search == replace:
        return {
            "qname": qname,
            "error": "no_change",
            "hint": "search and replace are identical; nothing to do.",
        }

    root_path = Path(root).resolve()
    idx = open_index(root_path, scope=None)
    try:
        canonical, paraphrase_hint = resolve_qname(idx, qname)
        if canonical is None:
            return {
                "qname": qname,
                "error": "not_found",
                "hint": f"No symbol {qname!r} in index. Run search first.",
            }
        row = idx.get_symbol(canonical)
        path = Path(row["file"])
        try:
            data = path.read_bytes()
        except OSError as e:
            return {"qname": canonical, "error": "read_failed", "hint": str(e)}

        # Same SHA-drift recovery as ``edit_symbol``: re-parse once on
        # mismatch and retry. Agents often run a chain of edits in
        # parallel; an autoformat-on-save between writes would
        # otherwise force a full re-query.
        if idx.current_sha(str(path)) != sha_bytes(data):
            if not refresh_file_in_index(idx, path, root_path):
                return {
                    "qname": canonical,
                    "error": "stale_coordinates",
                    "hint": (
                        f"File {str(path)!r} changed and could not be re-parsed."
                    ),
                }
            canonical2, _ = resolve_qname(idx, canonical)
            if canonical2 is None:
                return {
                    "qname": canonical,
                    "error": "not_found",
                    "hint": (
                        f"Symbol {canonical!r} no longer exists in {path.name!r} "
                        "after the external file change."
                    ),
                }
            row = idx.get_symbol(canonical2)
            canonical = canonical2

        text = data.decode("utf-8", errors="replace")
        had_trailing_nl = text.endswith("\n")
        lines = text.split("\n")
        if had_trailing_nl:
            lines.pop()

        ls, le = row["line_start"], row["line_end"]
        if ls < 1 or le > len(lines) or ls > le:
            return {
                "qname": canonical,
                "error": "stale_coordinates",
                "hint": (
                    f"Stored line range {ls}-{le} is outside the file "
                    f"(now {len(lines)} lines)."
                ),
            }

        body = "\n".join(lines[ls - 1 : le])
        match_count = body.count(search)
        if match_count == 0:
            return {
                "qname": canonical,
                "error": "not_found",
                "hint": (
                    f"search string not found in {canonical!r}'s body. "
                    f"Re-read with get_source({canonical!r}) and retry."
                ),
                "match": "search",
            }
        if match_count > 1:
            return {
                "qname": canonical,
                "error": "ambiguous",
                "hint": (
                    f"search string appears {match_count} times in "
                    f"{canonical!r}'s body. Widen it (add surrounding "
                    "context) until it's unique, then retry."
                ),
                "match_count": match_count,
            }

        new_body = body.replace(search, replace, 1)
        body_lines = new_body.split("\n")
        new_lines = lines[: ls - 1] + body_lines + lines[le:]
        new_text = "\n".join(new_lines)
        if had_trailing_nl:
            new_text += "\n"

        # Same pre-flight as edit_symbol — refuse to write a broken file.
        syn_err = _check_syntax(path, new_text)
        if syn_err is not None:
            return {
                "qname": canonical,
                "error": "syntax_error",
                "hint": (
                    f"Proposed edit would make {path.name!r} unparseable "
                    f"({syn_err}). Fix the replace and retry; nothing was written."
                ),
            }

        try:
            path.write_text(new_text, encoding="utf-8")
        except OSError as e:
            return {"qname": canonical, "error": "write_failed", "hint": str(e)}

        result: dict = {
            "qname": canonical,
            "file": str(path),
            "lines_before": f"{ls}-{le}",
            "lines_after": f"{ls}-{ls + len(body_lines) - 1}",
            "lines_replaced": le - ls + 1,
            "lines_written": len(body_lines),
            "chars_replaced": len(search),
        }
        if paraphrase_hint is not None:
            result["paraphrase_hint"] = (
                f"Resolved {qname!r} → {canonical!r} ({paraphrase_hint})."
            )
    finally:
        idx.close()

    from snapctx.api._indexer import index_root
    from snapctx.api._preload import invalidate_preloads
    refresh = index_root(root_path)
    invalidate_preloads(root_path)
    result["reindex"] = {
        "files_updated": refresh["files_updated"],
        "files_removed": refresh["files_removed"],
    }
    return result


def edit_symbol_search_replace_batch(
    edits: list[dict],
    root: str | Path = ".",
    scope: str | None = None,
) -> dict:
    """Apply many search/replace edits in one call.

    ``edits`` is a list of ``{"qname": str, "search": str, "replace": str}``
    dicts. Per-file atomicity, single re-index — same shape as
    ``edit_symbol_batch``.

    Multiple edits to the SAME symbol in one batch are applied in
    sequence (each search runs against the body produced by the
    previous edit), so the agent can chain "rename A → B, then change
    B's args" in one tool call.
    """
    if scope is not None:
        return {
            "error": "scope_unsupported",
            "hint": "edit_symbol_search_replace_batch does not support vendor scopes.",
        }
    if not edits:
        return {"applied": [], "errors": [], "files_touched": 0}

    # Fail-loud pre-pass: same reason as ``edit_symbol_batch`` — an
    # empty-symbol qname is silently destructive, so reject the whole
    # batch before any I/O.
    for edit in edits:
        q = edit.get("qname") if isinstance(edit, dict) else None
        if q is not None:
            validate_writable_qname(q)

    root_path = Path(root).resolve()
    idx = open_index(root_path, scope=None)
    applied: list[dict] = []
    errors: list[dict] = []

    # Phase 1: resolve every edit to a (file, line_start, line_end, qname).
    # Edits to the same qname are kept as a list (sequential apply).
    by_file: dict[str, list[dict]] = defaultdict(list)
    try:
        for edit in edits:
            qname = edit.get("qname")
            search = edit.get("search")
            replace = edit.get("replace")
            if not qname or search is None or replace is None:
                errors.append({
                    "qname": qname,
                    "error": "invalid_edit",
                    "hint": (
                        "each edit needs 'qname', 'search', and 'replace' keys."
                    ),
                })
                continue
            if search == replace:
                errors.append({
                    "qname": qname,
                    "error": "no_change",
                    "hint": "search and replace are identical; nothing to do.",
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
                "search": search,
                "replace": replace,
                "line_start": row["line_start"],
                "line_end": row["line_end"],
            })
    finally:
        idx.close()

    # Phase 2: per-file atomic apply. Group by symbol so multi-edit-per-symbol
    # works deterministically (sequential within a symbol, bottom-up across).
    for file_str, file_edits in by_file.items():
        path = Path(file_str)
        outcome = _apply_search_replace_to_file(path, file_edits, root_path)
        applied.extend(outcome["applied"])
        errors.extend(outcome["errors"])

    files_touched = sum(
        1 for f in by_file if any(a["file"] == f for a in applied)
    )

    if files_touched > 0:
        from snapctx.api._indexer import index_root
        from snapctx.api._preload import invalidate_preloads
        refresh = index_root(root_path)
        invalidate_preloads(root_path)
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


def _apply_search_replace_to_file(
    path: Path, edits: list[dict], root_path: Path,
) -> dict:
    """Apply all search/replace edits targeting one file.

    Per-symbol: chain searches in sequence. Across symbols: bottom-up
    (largest line_start first) so earlier line numbers stay valid.
    """
    applied: list[dict] = []
    errors: list[dict] = []

    # Group by qname so multi-edit-per-symbol is sequential.
    by_qname: dict[str, list[dict]] = defaultdict(list)
    for e in edits:
        by_qname[e["qname"]].append(e)

    # Detect overlaps across DIFFERENT symbols (same as edit_symbol_batch).
    distinct = list(by_qname.values())
    distinct.sort(key=lambda group: group[0]["line_start"])
    for prev_group, curr_group in zip(distinct, distinct[1:]):
        prev = prev_group[0]
        curr = curr_group[0]
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

    # Apply per-symbol. Sort the GROUPS bottom-up so unaffected line
    # numbers stay valid; within a group, apply in input order.
    distinct_desc = sorted(
        distinct, key=lambda g: g[0]["line_start"], reverse=True,
    )
    per_edit_outcome: list[dict] = []
    for group in distinct_desc:
        ls = group[0]["line_start"]
        le = group[0]["line_end"]
        if ls < 1 or le > len(lines) or ls > le:
            return {
                "applied": [],
                "errors": [{
                    "qname": group[0]["original_qname"],
                    "error": "stale_coordinates",
                    "hint": (
                        f"Stored line range {ls}-{le} for "
                        f"{group[0]['qname']!r} is outside the file "
                        f"(now {len(lines)} lines)."
                    ),
                }],
            }
        body = "\n".join(lines[ls - 1 : le])
        for e in group:
            count = body.count(e["search"])
            if count == 0:
                return {
                    "applied": [],
                    "errors": [{
                        "qname": e["original_qname"],
                        "error": "not_found",
                        "hint": (
                            f"search string not found in {e['qname']!r}'s body."
                        ),
                        "match": "search",
                    }],
                }
            if count > 1:
                return {
                    "applied": [],
                    "errors": [{
                        "qname": e["original_qname"],
                        "error": "ambiguous",
                        "hint": (
                            f"search string appears {count} times in "
                            f"{e['qname']!r}'s body."
                        ),
                        "match_count": count,
                    }],
                }
            body = body.replace(e["search"], e["replace"], 1)
            per_edit_outcome.append({
                "qname": e["qname"],
                "original_qname": e["original_qname"],
                "paraphrase_hint": e["paraphrase_hint"],
                "chars_replaced": len(e["search"]),
                "lines_before": f"{ls}-{le}",
            })
        body_lines = body.split("\n")
        lines = lines[: ls - 1] + body_lines + lines[le:]

    new_text = "\n".join(lines)
    if had_trailing_nl:
        new_text += "\n"

    syn_err = _check_syntax(path, new_text)
    if syn_err is not None:
        return {
            "applied": [],
            "errors": [{
                "qname": "<batch>",
                "file": str(path),
                "error": "syntax_error",
                "hint": (
                    f"Combined batch on {path.name!r} won't parse "
                    f"({syn_err}). No edits in this file were applied."
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

    for o in per_edit_outcome:
        entry = {
            "qname": o["qname"],
            "file": str(path),
            "chars_replaced": o["chars_replaced"],
            "lines_before": o["lines_before"],
        }
        if o["paraphrase_hint"] is not None:
            entry["paraphrase_hint"] = (
                f"Resolved {o['original_qname']!r} → {o['qname']!r} "
                f"({o['paraphrase_hint']})."
            )
        applied.append(entry)
    return {"applied": applied, "errors": []}


def _check_syntax(path: Path, new_text: str) -> str | None:
    """Run the same syntax pre-flight ``edit_symbol`` does.

    Returns ``None`` on a clean parse, or a short error description
    suitable for a ``hint`` string on failure. We share this between
    the single-edit and batch paths to keep error messages identical.
    """
    if path.suffix in _PYTHON_SUFFIXES:
        try:
            ast.parse(new_text)
        except SyntaxError as e:
            return f"{e.msg} at line {e.lineno}, col {e.offset}"
        return None
    if path.suffix in _TS_SUFFIXES:
        from snapctx.parsers.typescript import find_syntax_error
        err = find_syntax_error(new_text, path.suffix)
        if err is not None:
            line, col = err
            return f"tree-sitter error at line {line}, col {col}"
    return None
