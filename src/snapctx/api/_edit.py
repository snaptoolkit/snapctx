"""Symbol-level write op: ``edit_symbol`` (replace a symbol's body by qname).

Mirror of ``get_source`` for the write side. The agent already knows a
qname from a prior search/source call — it shouldn't need to re-read the
whole file just to compute byte offsets for an edit.

Contract: query first (``search`` / ``source``) so coordinates are
fresh, then edit. If the file changed since the last index (any
external editor, manual git operation, etc.) we refuse the write and
ask the caller to re-query — line numbers may have drifted.

Re-indexes the modified file before returning, so a follow-up
``get_source`` immediately reflects the edit.
"""

from __future__ import annotations

import ast
from pathlib import Path

from snapctx.api._common import open_index, refresh_file_in_index, resolve_qname
from snapctx.index import sha_bytes

# Suffixes for which we run a Python AST parse on the candidate file
# before writing.
_PYTHON_SUFFIXES = (".py", ".pyi")

# Suffixes for which we run a tree-sitter syntax check (looks for ERROR /
# MISSING nodes in the parse tree). Tree-sitter is permissive — it
# always returns a tree — but it does flag truly broken syntax via
# ``Node.has_error``.
_TS_SUFFIXES = (".ts", ".tsx", ".mts", ".cts", ".jsx", ".js", ".mjs", ".cjs")


def delete_symbol(
    qname: str,
    root: str | Path = ".",
    scope: str | None = None,
) -> dict:
    """Remove a symbol from its file (deletes the line range).

    Mirror of ``edit_symbol`` for the "drop this entirely" case.
    Trims one leading blank line if present so we don't leave a
    double-blank gap between the prior and following symbols.
    Same staleness + syntax pre-flight as ``edit_symbol``.

    Returns ``{"qname", "file", "lines_deleted", "reindex"}`` or
    a structured error.
    """
    if scope is not None:
        return {
            "qname": qname,
            "error": "scope_unsupported",
            "hint": "delete_symbol does not support vendor scopes.",
        }
    root_path = Path(root).resolve()
    idx = open_index(root_path, scope=None)
    try:
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
            data = path.read_bytes()
        except OSError as e:
            return {"qname": canonical, "error": "read_failed", "hint": str(e)}

        # Auto-recovery for SHA drift: if the file changed since the
        # last index pass (autoformat-on-save, IDE write, parallel
        # tool), re-parse it once and retry the lookup. Beats forcing
        # the agent to re-query manually for every same-file edit.
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
                        "after the external file change. Re-query for the new qname."
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

        # Drop one leading blank line if present, so we don't end up
        # with a double-blank gap. (PEP-8 spacing has 2 blank lines
        # *between* top-level fns; removing a fn should leave 2, not 3.)
        delete_from = ls - 1
        delete_to = le  # exclusive in slice terms
        if delete_from > 0 and lines[delete_from - 1].strip() == "":
            delete_from -= 1

        new_lines = lines[:delete_from] + lines[delete_to:]
        new_text = "\n".join(new_lines)
        if had_trailing_nl:
            new_text += "\n"

        # Syntax pre-flight on the file post-deletion.
        if path.suffix in _PYTHON_SUFFIXES:
            try:
                ast.parse(new_text)
            except SyntaxError as e:
                return {
                    "qname": canonical,
                    "error": "syntax_error",
                    "hint": (
                        f"Deleting {canonical!r} would make {path.name!r} "
                        f"unparseable: {e.msg} at line {e.lineno}, col "
                        f"{e.offset}. Nothing written."
                    ),
                }
        elif path.suffix in _TS_SUFFIXES:
            from snapctx.parsers.typescript import find_syntax_error
            err = find_syntax_error(new_text, path.suffix)
            if err is not None:
                line, col = err
                return {
                    "qname": canonical,
                    "error": "syntax_error",
                    "hint": (
                        f"Deleting {canonical!r} would make {path.name!r} "
                        f"unparseable (tree-sitter at line {line}, col {col})."
                    ),
                }

        try:
            path.write_text(new_text, encoding="utf-8")
        except OSError as e:
            return {"qname": canonical, "error": "write_failed", "hint": str(e)}

        result: dict = {
            "qname": canonical,
            "file": str(path),
            "lines_deleted": delete_to - delete_from,
            "deleted_range": f"{delete_from + 1}-{delete_to}",
        }
        if paraphrase_hint is not None:
            result["paraphrase_hint"] = (
                f"Resolved {qname!r} → {canonical!r} ({paraphrase_hint})."
            )
    finally:
        idx.close()

    from snapctx.api._indexer import index_root
    refresh = index_root(root_path)
    result["reindex"] = {
        "files_updated": refresh["files_updated"],
        "files_removed": refresh["files_removed"],
    }
    return result


def edit_symbol(
    qname: str,
    new_body: str,
    root: str | Path = ".",
    scope: str | None = None,
) -> dict:
    """Replace the source body of ``qname`` with ``new_body``.

    The replacement covers the inclusive line range ``[line_start, line_end]``
    stored for the symbol — the same range ``get_source`` returns. The
    caller is responsible for indentation: ``new_body`` is spliced in
    verbatim, including its leading whitespace on each line.

    Trailing newlines on ``new_body`` are normalized — the splice always
    leaves the file with exactly the original line ending after the
    edited region.

    Returns ``{"qname", "file", "lines_before", "lines_after",
    "lines_replaced"}`` on success, or ``{"qname", "error", "hint"}`` on
    failure. Failure modes:

    * ``not_found`` — qname is not in the index (after paraphrase
      fallback).
    * ``stale_coordinates`` — the file's SHA on disk doesn't match the
      one recorded at index time. Line numbers may have drifted; the
      agent should re-query (``source`` / ``find``) and retry.
    * ``read_failed`` / ``write_failed`` — filesystem error; message
      contains the underlying exception.
    """
    if scope is not None:
        return {
            "qname": qname,
            "error": "scope_unsupported",
            "hint": "edit_symbol does not support vendor scopes — vendored packages are read-only.",
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

        # Auto-recovery for SHA drift: re-parse the file once and retry
        # the qname lookup before giving up. Covers the common case
        # where an autoformat-on-save / IDE write / parallel tool
        # touched the file between two snapctx writes.
        indexed_sha = idx.current_sha(str(path))
        if indexed_sha != sha_bytes(data):
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
                        "after the external file change. Re-query for the new qname."
                    ),
                }
            row = idx.get_symbol(canonical2)
            canonical = canonical2

        text = data.decode("utf-8", errors="replace")
        # Splitlines drops the trailing newline distinction; rebuild with
        # the original newline style by tracking line endings explicitly.
        # Conservative: assume "\n" newlines (the only style snapctx
        # parses), and preserve the file's trailing-newline state.
        had_trailing_nl = text.endswith("\n")
        lines = text.split("\n")
        if had_trailing_nl:
            # Splitting "a\n" by "\n" gives ["a", ""] — drop the empty
            # tail so indexing matches the symbol's 1-based line range.
            lines.pop()

        # Symbol coordinates are 1-based, inclusive on both ends.
        ls = row["line_start"]
        le = row["line_end"]
        if ls < 1 or le > len(lines) or ls > le:
            return {
                "qname": canonical,
                "error": "stale_coordinates",
                "hint": (
                    f"Stored line range {ls}-{le} is outside the file "
                    f"(now {len(lines)} lines). Re-query and retry."
                ),
            }
        lines_replaced = le - ls + 1

        # Normalize new_body: strip a single trailing newline so we
        # don't double-up the line terminator at the splice boundary.
        body_lines = new_body.split("\n")
        if body_lines and body_lines[-1] == "":
            body_lines.pop()

        new_lines = lines[: ls - 1] + body_lines + lines[le:]
        new_text = "\n".join(new_lines)
        if had_trailing_nl:
            new_text += "\n"

        # Syntax pre-flight: refuse to write a file that won't parse.
        # Without this, a bad replacement (mismatched indent, dangling
        # colon, missing brace) silently breaks the file and the next
        # query goes to a corrupted index.
        if path.suffix in _PYTHON_SUFFIXES:
            try:
                ast.parse(new_text)
            except SyntaxError as e:
                return {
                    "qname": canonical,
                    "error": "syntax_error",
                    "hint": (
                        f"Proposed edit would make {path.name!r} unparseable: "
                        f"{e.msg} at line {e.lineno}, col {e.offset}. "
                        "Fix the new_body and retry; nothing was written."
                    ),
                }
        elif path.suffix in _TS_SUFFIXES:
            from snapctx.parsers.typescript import find_syntax_error
            err = find_syntax_error(new_text, path.suffix)
            if err is not None:
                line, col = err
                return {
                    "qname": canonical,
                    "error": "syntax_error",
                    "hint": (
                        f"Proposed edit would make {path.name!r} unparseable "
                        f"(tree-sitter reports an error at line {line}, "
                        f"col {col}). Fix the new_body and retry; nothing "
                        "was written."
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
            "lines_after": f"{ls}-{ls + len(body_lines) - 1}" if body_lines else f"{ls}-{ls - 1}",
            "lines_replaced": lines_replaced,
            "lines_written": len(body_lines),
        }
        if paraphrase_hint is not None:
            result["paraphrase_hint"] = (
                f"Resolved {qname!r} → {canonical!r} ({paraphrase_hint}). "
                f"Use {canonical!r} verbatim in subsequent calls."
            )
    finally:
        idx.close()

    # Re-index the file so subsequent queries (including same-process)
    # see the new coordinates. SHA-keyed and only the one file changed,
    # so this is fast.
    from snapctx.api._indexer import index_root

    refresh = index_root(root_path)
    result["reindex"] = {
        "files_updated": refresh["files_updated"],
        "files_removed": refresh["files_removed"],
    }
    return result
