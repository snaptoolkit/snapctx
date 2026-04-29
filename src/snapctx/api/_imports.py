"""Imports — add or remove a single import line in a file.

Imports live ABOVE the first symbol in most files, which means
``edit_symbol`` cannot reach them — its splice operates on the line
range of an indexed symbol. This module fills that gap with two
focused ops:

* ``add_import(file, statement, root)`` — add a new import line.
  Idempotent (no-op if the exact statement is already present).
  Appends at the bottom of the existing import block, or inserts
  at the top of the file when there are no imports yet.

* ``remove_import(file, statement, root)`` — remove the line whose
  text matches the given statement (after stripping). No-op if not
  found, so the call is safe to issue speculatively.

Both run the same SHA-staleness guard as ``edit_symbol`` and the
same Python / TS syntax pre-flight on the candidate file. After
a successful write, the file is re-indexed so the ``imports``
table reflects the change.
"""

from __future__ import annotations

import ast
from pathlib import Path

from snapctx.api._common import open_index, refresh_file_in_index
from snapctx.index import sha_bytes

_PYTHON_SUFFIXES = (".py", ".pyi")
_TS_SUFFIXES = (".ts", ".tsx", ".mts", ".cts", ".jsx", ".js", ".mjs", ".cjs")


def _resolve_file(file: str, root_path: Path) -> Path | None:
    """Resolve a file argument to an absolute path inside the repo.

    Accepts an absolute path, a path relative to ``root``, or one
    that ``imports.file`` (always absolute) would store. Returns the
    resolved Path if it exists under root, else None.
    """
    p = Path(file)
    if p.is_absolute():
        return p if p.exists() else None
    abs_p = (root_path / p).resolve()
    return abs_p if abs_p.exists() else None


def _import_block_lines(idx, abs_file: str) -> list[int]:
    rows = idx.conn.execute(
        "SELECT DISTINCT line FROM imports WHERE file = ? ORDER BY line",
        (abs_file,),
    ).fetchall()
    return [r["line"] for r in rows]


def _post_docstring_insert_index(path: Path, lines: list[str]) -> int:
    """Pick the right insert index for a NEW import in a file with no imports.

    For Python, prefer "right after a leading module docstring" so the
    docstring stays at the top of the file (Python convention). For
    everything else, top-of-file (index 0).

    Returns a 0-based index — ``new_lines[idx]`` is where the new
    import lands. Falls back to 0 on any parse error.
    """
    if path.suffix not in _PYTHON_SUFFIXES:
        return 0
    # Cheap shortcut: if the file is empty or doesn't start with a
    # quote-like char, no docstring.
    text_head = "\n".join(lines[:1]).lstrip()
    if not text_head or text_head[0] not in ("'", '"'):
        return 0
    try:
        tree = ast.parse("\n".join(lines))
    except SyntaxError:
        return 0
    if not tree.body:
        return 0
    first = tree.body[0]
    if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
            and isinstance(first.value.value, str):
        # ast.end_lineno is 1-based; we want a 0-based index pointing
        # to the line AFTER the docstring.
        end = first.value.end_lineno or first.value.lineno or 0
        return end
    return 0


def _validate_syntax(path: Path, new_text: str) -> dict | None:
    """Return an error dict if the candidate file won't parse, else None."""
    if path.suffix in _PYTHON_SUFFIXES:
        try:
            ast.parse(new_text)
        except SyntaxError as e:
            return {
                "error": "syntax_error",
                "hint": (
                    f"Edit would make {path.name!r} unparseable: "
                    f"{e.msg} at line {e.lineno}, col {e.offset}. Nothing written."
                ),
            }
    elif path.suffix in _TS_SUFFIXES:
        from snapctx.parsers.typescript import find_syntax_error
        err = find_syntax_error(new_text, path.suffix)
        if err is not None:
            line, col = err
            return {
                "error": "syntax_error",
                "hint": (
                    f"Edit would make {path.name!r} unparseable "
                    f"(tree-sitter error at line {line}, col {col}). "
                    "Nothing written."
                ),
            }
    return None


def _write_and_reindex(path: Path, new_text: str, root_path: Path) -> dict:
    err = _validate_syntax(path, new_text)
    if err is not None:
        return err
    try:
        path.write_text(new_text, encoding="utf-8")
    except OSError as e:
        return {"error": "write_failed", "hint": str(e)}
    from snapctx.api._indexer import index_root
    refresh = index_root(root_path)
    return {
        "reindex": {
            "files_updated": refresh["files_updated"],
            "files_removed": refresh["files_removed"],
        }
    }


def add_import(
    file: str,
    statement: str,
    root: str | Path = ".",
    scope: str | None = None,
) -> dict:
    """Add ``statement`` as a new import line in ``file``.

    ``statement`` is the raw import line (``"import os"`` /
    ``"from pathlib import Path"`` / ``'import { x } from "y"'``).
    Trailing whitespace and newlines are stripped before splice.

    Idempotent: if a line whose stripped form equals the statement
    already exists in the file, no write happens and the result
    has ``"already_present": True``.

    Returns ``{"file", "inserted_at_line", "statement", "reindex"}``
    on success; ``{"error", "hint"}`` on failure.
    """
    if scope is not None:
        return {
            "error": "scope_unsupported",
            "hint": "add_import does not support vendor scopes.",
        }
    statement = statement.rstrip("\n").rstrip()
    if not statement:
        return {"error": "invalid_statement", "hint": "statement is empty."}

    root_path = Path(root).resolve()
    path = _resolve_file(file, root_path)
    if path is None:
        return {"error": "not_found", "hint": f"File {file!r} not under root {str(root_path)!r}."}

    idx = open_index(root_path, scope=None)
    try:
        try:
            data = path.read_bytes()
        except OSError as e:
            return {"error": "read_failed", "hint": str(e)}
        # Auto-recovery: re-parse once if SHA drifted (autoformat,
        # IDE write, parallel tool) instead of bouncing the agent
        # through a re-query loop.
        if idx.current_sha(str(path)) != sha_bytes(data):
            if not refresh_file_in_index(idx, path, root_path):
                return {
                    "error": "stale_coordinates",
                    "hint": (
                        f"File {str(path)!r} changed and could not be re-parsed."
                    ),
                }

        text = data.decode("utf-8", errors="replace")
        had_trailing_nl = text.endswith("\n")
        lines = text.split("\n")
        if had_trailing_nl:
            lines.pop()

        # Idempotency check.
        if any(ln.strip() == statement for ln in lines):
            return {
                "file": str(path),
                "statement": statement,
                "already_present": True,
            }

        block_lines = _import_block_lines(idx, str(path))
        if block_lines:
            # Append directly after the last existing import line.
            insert_idx = max(block_lines)  # 1-based last import line; insert AFTER
        else:
            # No existing imports: insert AFTER a leading module
            # docstring if there is one (Python convention), else at
            # the top of the file. For TS/JS we always pick top-of-file
            # — there's no docstring concept and tree-sitter would
            # accept either placement.
            insert_idx = _post_docstring_insert_index(path, lines)
    finally:
        idx.close()

    new_lines = lines[:insert_idx] + [statement] + lines[insert_idx:]
    new_text = "\n".join(new_lines)
    if had_trailing_nl:
        new_text += "\n"

    write_result = _write_and_reindex(path, new_text, root_path)
    if "error" in write_result:
        return write_result
    return {
        "file": str(path),
        "statement": statement,
        "inserted_at_line": insert_idx + 1,
        "already_present": False,
        **write_result,
    }


def remove_import(
    file: str,
    statement: str,
    root: str | Path = ".",
    scope: str | None = None,
) -> dict:
    """Remove the line whose stripped form equals ``statement``.

    No-op if not found (returns ``"already_absent": True``). Useful
    after a rename: ``remove_import(file, old)`` followed by
    ``add_import(file, new)``.
    """
    if scope is not None:
        return {
            "error": "scope_unsupported",
            "hint": "remove_import does not support vendor scopes.",
        }
    statement = statement.rstrip("\n").rstrip()
    if not statement:
        return {"error": "invalid_statement", "hint": "statement is empty."}

    root_path = Path(root).resolve()
    path = _resolve_file(file, root_path)
    if path is None:
        return {"error": "not_found", "hint": f"File {file!r} not under root {str(root_path)!r}."}

    idx = open_index(root_path, scope=None)
    try:
        try:
            data = path.read_bytes()
        except OSError as e:
            return {"error": "read_failed", "hint": str(e)}
        if idx.current_sha(str(path)) != sha_bytes(data):
            if not refresh_file_in_index(idx, path, root_path):
                return {
                    "error": "stale_coordinates",
                    "hint": (
                        f"File {str(path)!r} changed and could not be re-parsed."
                    ),
                }
    finally:
        idx.close()

    text = data.decode("utf-8", errors="replace")
    had_trailing_nl = text.endswith("\n")
    lines = text.split("\n")
    if had_trailing_nl:
        lines.pop()

    keep = [ln for ln in lines if ln.strip() != statement]
    if len(keep) == len(lines):
        return {
            "file": str(path),
            "statement": statement,
            "already_absent": True,
        }

    new_text = "\n".join(keep)
    if had_trailing_nl:
        new_text += "\n"

    write_result = _write_and_reindex(path, new_text, root_path)
    if "error" in write_result:
        return write_result
    return {
        "file": str(path),
        "statement": statement,
        "lines_removed": len(lines) - len(keep),
        "already_absent": False,
        **write_result,
    }
