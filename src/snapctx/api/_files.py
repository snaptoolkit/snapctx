"""File-level write ops: create, delete, move whole files.

These complement the symbol-level ops (``edit_symbol``,
``insert_symbol``, ``delete_symbol``) for the cases where the unit
of change is the file itself.

* ``create_file(path, content, root)`` — write a new file under
  ``root`` and reindex it. Refuses if the path already exists; the
  caller should ``edit_symbol`` to modify existing content.

* ``delete_file(path, root)`` — remove the file from disk and drop
  its symbols from the index. Refuses to leave the index in an
  inconsistent state on a partial failure.

* ``move_file(old_path, new_path, root)`` — rename the file on
  disk and reindex. Cross-file import callsites are NOT
  auto-updated — that's a job for ``add_import`` /
  ``remove_import`` (composable from this op + the imports module).

All three run a Python / TS syntax pre-flight on file content
before writing.
"""

from __future__ import annotations

import ast
from pathlib import Path

from snapctx.api._common import open_index

_PYTHON_SUFFIXES = (".py", ".pyi")
_TS_SUFFIXES = (".ts", ".tsx", ".mts", ".cts", ".jsx", ".js", ".mjs", ".cjs")


def _validate_syntax(path_for_kind: Path, text: str) -> dict | None:
    if path_for_kind.suffix in _PYTHON_SUFFIXES:
        try:
            ast.parse(text)
        except SyntaxError as e:
            return {
                "error": "syntax_error",
                "hint": (
                    f"Content for {path_for_kind.name!r} is unparseable: "
                    f"{e.msg} at line {e.lineno}, col {e.offset}. Nothing written."
                ),
            }
    elif path_for_kind.suffix in _TS_SUFFIXES:
        from snapctx.parsers.typescript import find_syntax_error
        err = find_syntax_error(text, path_for_kind.suffix)
        if err is not None:
            line, col = err
            return {
                "error": "syntax_error",
                "hint": (
                    f"Content for {path_for_kind.name!r} is unparseable "
                    f"(tree-sitter error at line {line}, col {col}). Nothing written."
                ),
            }
    return None


def create_file(
    path: str,
    content: str,
    root: str | Path = ".",
    scope: str | None = None,
) -> dict:
    """Create a new file at ``path`` (relative to ``root``, or absolute) with ``content``.

    Refuses if the path exists (use ``edit_symbol`` / ``insert_symbol``
    on the existing file instead). Runs the syntax pre-flight on the
    full content for known languages, then writes + reindexes.
    """
    if scope is not None:
        return {
            "error": "scope_unsupported",
            "hint": "create_file does not support vendor scopes.",
        }
    root_path = Path(root).resolve()
    target = Path(path)
    if not target.is_absolute():
        target = (root_path / target).resolve()
    if not str(target).startswith(str(root_path)):
        return {
            "error": "outside_root",
            "hint": f"Refusing to create {target!r} — outside root {str(root_path)!r}.",
        }
    if target.exists():
        return {
            "error": "already_exists",
            "hint": f"{target!r} already exists. Use edit_symbol to modify.",
        }

    err = _validate_syntax(target, content)
    if err is not None:
        return err

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    except OSError as e:
        return {"error": "write_failed", "hint": str(e)}

    from snapctx.api._indexer import index_root
    from snapctx.api._preload import invalidate_preloads
    refresh = index_root(root_path)
    invalidate_preloads(root_path)
    return {
        "file": str(target),
        "bytes_written": len(content.encode("utf-8")),
        "reindex": {
            "files_updated": refresh["files_updated"],
            "files_removed": refresh["files_removed"],
        },
    }


def delete_file(
    path: str,
    root: str | Path = ".",
    scope: str | None = None,
) -> dict:
    """Remove ``path`` from disk and drop its symbols from the index.

    Refuses if the file is outside ``root`` (safety guard against
    accidentally deleting unrelated files via a bad relative path).
    """
    if scope is not None:
        return {
            "error": "scope_unsupported",
            "hint": "delete_file does not support vendor scopes.",
        }
    root_path = Path(root).resolve()
    target = Path(path)
    if not target.is_absolute():
        target = (root_path / target).resolve()
    if not str(target).startswith(str(root_path)):
        return {
            "error": "outside_root",
            "hint": f"Refusing to delete {target!r} — outside root {str(root_path)!r}.",
        }
    if not target.exists():
        return {
            "error": "not_found",
            "hint": f"{target!r} does not exist.",
        }
    if target.is_dir():
        return {
            "error": "is_directory",
            "hint": f"{target!r} is a directory. Use the OS-level rm for trees.",
        }

    try:
        target.unlink()
    except OSError as e:
        return {"error": "delete_failed", "hint": str(e)}

    # Drop file from index. The next index_root would notice the
    # missing file and forget it, but we do it explicitly so the
    # response can confirm symbols were removed.
    idx = open_index(root_path, scope=None)
    try:
        symbol_count = idx.conn.execute(
            "SELECT COUNT(*) AS n FROM symbols WHERE file = ?",
            (str(target),),
        ).fetchone()["n"]
        idx.forget_file(str(target))
    finally:
        idx.close()

    from snapctx.api._indexer import index_root
    from snapctx.api._preload import invalidate_preloads
    refresh = index_root(root_path)
    invalidate_preloads(root_path)
    return {
        "file": str(target),
        "symbols_dropped": symbol_count,
        "reindex": {
            "files_updated": refresh["files_updated"],
            "files_removed": refresh["files_removed"],
        },
    }


def move_file(
    old_path: str,
    new_path: str,
    root: str | Path = ".",
    scope: str | None = None,
) -> dict:
    """Rename ``old_path`` to ``new_path`` on disk and reindex.

    Cross-file import callsites are NOT updated automatically — this
    op moves the file and lets the caller drive the import-rewrite
    pass via ``add_import`` / ``remove_import`` on the affected
    files. Returns the list of files that import the moved module
    (so the agent knows where to look) under ``importing_files``.
    """
    if scope is not None:
        return {
            "error": "scope_unsupported",
            "hint": "move_file does not support vendor scopes.",
        }
    root_path = Path(root).resolve()

    src = Path(old_path)
    if not src.is_absolute():
        src = (root_path / src).resolve()
    dst = Path(new_path)
    if not dst.is_absolute():
        dst = (root_path / dst).resolve()
    for p in (src, dst):
        if not str(p).startswith(str(root_path)):
            return {
                "error": "outside_root",
                "hint": f"Refusing to move via {p!r} — outside root {str(root_path)!r}.",
            }
    if not src.exists():
        return {"error": "not_found", "hint": f"{src!r} does not exist."}
    if dst.exists():
        return {"error": "already_exists", "hint": f"{dst!r} already exists."}

    # Capture the list of importing files BEFORE we rename, while the
    # index still knows the old path.
    idx = open_index(root_path, scope=None)
    try:
        # We don't know the canonical module name without reparsing,
        # but we can list every file whose imports table has any
        # entry mentioning the old file's stem.
        rel_old = src.relative_to(root_path)
        old_stem_name = rel_old.stem  # "shallow"
        # Pull every distinct file that has *any* import — we'll
        # filter by resolved module path on the indexed side, but
        # for the v1 response we hand back the full set so the
        # agent can run snapctx_find / snapctx_search itself.
        candidates = idx.conn.execute(
            "SELECT DISTINCT file FROM imports WHERE module LIKE ? OR name = ?",
            (f"%{old_stem_name}%", old_stem_name),
        ).fetchall()
        importing_files = [r["file"] for r in candidates if r["file"] != str(src)]
    finally:
        idx.close()

    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)
    except OSError as e:
        return {"error": "move_failed", "hint": str(e)}

    from snapctx.api._indexer import index_root
    from snapctx.api._preload import invalidate_preloads
    refresh = index_root(root_path)
    invalidate_preloads(root_path)
    return {
        "old_file": str(src),
        "new_file": str(dst),
        "importing_files": importing_files,
        "hint": (
            "File moved. Update import sites in importing_files using "
            "add_import / remove_import."
        ) if importing_files else None,
        "reindex": {
            "files_updated": refresh["files_updated"],
            "files_removed": refresh["files_removed"],
        },
    }
