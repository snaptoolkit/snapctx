"""Insert a new top-level symbol relative to an existing one.

``edit_symbol`` only replaces existing symbols — useful when you know
*what* to change, useless when you need to *add* a brand-new function
or class. ``insert_symbol`` fills that gap: pick an existing anchor
qname, say "before" or "after", and the new text is spliced into the
file at the anchor's line boundary.

Same staleness guarantee as ``edit_symbol``: the file's SHA on disk
must match the one recorded at index time, otherwise the line range
of the anchor may have drifted and we refuse to splice.

Caller's job:

* The ``new_text`` is inserted as-is, including its leading and
  trailing whitespace. Two blank lines on either side gets you
  PEP-8 spacing for top-level functions.
* The text is added at the line BEFORE the anchor's ``line_start``
  (``position="before"``) or AFTER its ``line_end``
  (``position="after"``, the default) — so it lands at the same
  indentation as the anchor.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Literal

from snapctx.api._common import open_index, resolve_qname
from snapctx.index import sha_bytes
from snapctx.qname import validate_writable_qname

_PYTHON_SUFFIXES = (".py", ".pyi")
_TS_SUFFIXES = (".ts", ".tsx", ".mts", ".cts", ".jsx", ".js", ".mjs", ".cjs")


def insert_symbol(
    anchor_qname: str,
    new_text: str,
    root: str | Path = ".",
    position: Literal["before", "after"] = "after",
    scope: str | None = None,
) -> dict:
    """Insert ``new_text`` adjacent to ``anchor_qname`` in its file.

    Returns ``{"anchor", "file", "anchor_lines", "inserted_at",
    "lines_inserted", "reindex"}`` on success, or ``{"anchor",
    "error", "hint"}`` on failure. Failure modes:

    * ``not_found`` — anchor qname doesn't resolve.
    * ``stale_coordinates`` — file changed since indexing.
    * ``read_failed`` / ``write_failed`` — filesystem error.
    * ``invalid_position`` — ``position`` not ``"before"`` or ``"after"``.
    * ``scope_unsupported`` — vendor scopes are read-only.

    Raises ``ValueError`` if ``anchor_qname`` is empty, malformed, or
    has an empty symbol after the colon.
    """
    validate_writable_qname(anchor_qname)
    if scope is not None:
        return {
            "anchor": anchor_qname,
            "error": "scope_unsupported",
            "hint": "insert_symbol does not support vendor scopes — vendored packages are read-only.",
        }
    if position not in ("before", "after"):
        return {
            "anchor": anchor_qname,
            "error": "invalid_position",
            "hint": f"position must be 'before' or 'after', got {position!r}.",
        }

    root_path = Path(root).resolve()
    idx = open_index(root_path, scope=None)
    try:
        canonical, paraphrase_hint = resolve_qname(idx, anchor_qname)
        if canonical is None:
            return {
                "anchor": anchor_qname,
                "error": "not_found",
                "hint": f"No symbol {anchor_qname!r} in index. Run search first.",
            }
        row = idx.get_symbol(canonical)
        path = Path(row["file"])
        try:
            data = path.read_bytes()
        except OSError as e:
            return {"anchor": canonical, "error": "read_failed", "hint": str(e)}

        if idx.current_sha(str(path)) != sha_bytes(data):
            return {
                "anchor": canonical,
                "error": "stale_coordinates",
                "hint": (
                    f"File {str(path)!r} has changed since the last index. "
                    "Re-query and retry."
                ),
            }

        text = data.decode("utf-8", errors="replace")
        had_trailing_nl = text.endswith("\n")
        lines = text.split("\n")
        if had_trailing_nl:
            lines.pop()

        ls, le = row["line_start"], row["line_end"]
        if ls < 1 or le > len(lines) or ls > le:
            return {
                "anchor": canonical,
                "error": "stale_coordinates",
                "hint": (
                    f"Stored line range {ls}-{le} is outside the file "
                    f"(now {len(lines)} lines)."
                ),
            }

        # Splice point: 0-based index where new lines should land.
        if position == "before":
            splice_at = ls - 1
        else:
            splice_at = le

        new_lines = new_text.split("\n")
        if new_lines and new_lines[-1] == "":
            # Drop the trailing empty entry from a body that ends with "\n".
            new_lines.pop()
        # Strip caller-supplied leading/trailing blank lines so they can't
        # double-up with the separator we add below. Agents often emit a
        # bare ``def foo():\n    ...`` without padding; we'd otherwise
        # land it directly against the anchor (PEP 8 violation, no
        # syntax error so silent until review).
        while new_lines and new_lines[0].strip() == "":
            new_lines.pop(0)
        while new_lines and new_lines[-1].strip() == "":
            new_lines.pop()

        # Pick the right inter-symbol gap based on language + nesting:
        # PEP 8 wants 2 blanks between top-level defs, 1 between methods.
        # TS/JS communities settle on 1 either way; markdown / configs
        # don't have a convention so 1 is the safe default.
        if path.suffix in _PYTHON_SUFFIXES:
            gap = 1 if row["parent_qname"] else 2
        elif path.suffix in _TS_SUFFIXES:
            gap = 1
        else:
            gap = 1

        if position == "after":
            before_part = list(lines[:splice_at])
            after_part = list(lines[splice_at:])
            # Eat any leading blanks that already sit between the anchor
            # and the next symbol — we'll re-emit exactly ``gap`` of them.
            while after_part and after_part[0].strip() == "":
                after_part.pop(0)
            out = before_part + ([""] * gap if before_part else []) + new_lines
            if after_part:
                out += [""] * gap + after_part
        else:  # before
            before_part = list(lines[:splice_at])
            after_part = list(lines[splice_at:])
            # Eat trailing blanks that sat between the prior content and
            # the anchor — we'll re-emit exactly ``gap`` of them.
            while before_part and before_part[-1].strip() == "":
                before_part.pop()
            out = before_part
            if before_part:
                out += [""] * gap
            out += new_lines + [""] * gap + after_part
        new_file_text = "\n".join(out)
        if had_trailing_nl:
            new_file_text += "\n"

        # Syntax pre-flight — refuse to write a broken file.
        if path.suffix in _PYTHON_SUFFIXES:
            try:
                ast.parse(new_file_text)
            except SyntaxError as e:
                return {
                    "anchor": canonical,
                    "error": "syntax_error",
                    "hint": (
                        f"Proposed insert would make {path.name!r} unparseable: "
                        f"{e.msg} at line {e.lineno}, col {e.offset}. "
                        "Fix the new_text and retry; nothing was written."
                    ),
                }
        elif path.suffix in _TS_SUFFIXES:
            from snapctx.parsers.typescript import find_syntax_error
            err = find_syntax_error(new_file_text, path.suffix)
            if err is not None:
                line, col = err
                return {
                    "anchor": canonical,
                    "error": "syntax_error",
                    "hint": (
                        f"Proposed insert would make {path.name!r} unparseable "
                        f"(tree-sitter reports an error at line {line}, "
                        f"col {col}). Fix the new_text and retry; nothing "
                        "was written."
                    ),
                }

        try:
            path.write_text(new_file_text, encoding="utf-8")
        except OSError as e:
            return {"anchor": canonical, "error": "write_failed", "hint": str(e)}

        result: dict = {
            "anchor": canonical,
            "file": str(path),
            "anchor_lines": f"{ls}-{le}",
            "position": position,
            "inserted_at": f"{splice_at + 1}-{splice_at + len(new_lines)}" if new_lines else f"{splice_at + 1}-{splice_at}",
            "lines_inserted": len(new_lines),
        }
        if paraphrase_hint is not None:
            result["paraphrase_hint"] = (
                f"Resolved {anchor_qname!r} → {canonical!r} ({paraphrase_hint})."
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
