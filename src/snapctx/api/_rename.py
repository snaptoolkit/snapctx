"""Coordinated rename: change a symbol's name + every call site + every import.

Today the agent can do this by hand: ``find`` the literal, then for
each caller's qname call ``edit_symbol`` with a body that has the old
name swapped for the new. ``rename_symbol`` collapses that into one
op so the latency cost (and the bookkeeping the model has to do) drops.

What it does:

1. Resolve ``old_qname`` to a canonical qname + the file that owns it.
2. Compute ``new_qname`` (same module path + ``new_name``) and refuse
   if a symbol with that qname already exists.
3. For every symbol whose body mentions the old name (the def itself
   plus every caller), build a new body with the old name replaced by
   the new name on word boundaries, and queue an edit for the batch.
4. For every import line that names the old symbol via ``from … import
   old_name`` (or ``import old_name``), rewrite just that line.
5. Apply the body edits via ``edit_symbol_batch`` (per-file atomic +
   syntax pre-flight). Apply the import line rewrites file-by-file.
6. Re-index and return a structured summary of every site changed.

Punts deliberately:

* Wildcard imports (``from foo import *``) — no way to know if the
  importer references the symbol by name.
* Aliased imports (``from foo import bar as baz``) — would need to
  rewrite every usage of ``baz`` in that file, which is the agent's
  job (call ``rename_symbol`` again with ``baz`` → ``new_baz``).
* Cross-language renames — limited to the language that owns the def.
"""

from __future__ import annotations

import re
from pathlib import Path

from snapctx.api._common import open_index, resolve_qname
from snapctx.api._edit_batch import edit_symbol_batch
from snapctx.index import sha_bytes


def rename_symbol(
    old_qname: str,
    new_name: str,
    root: str | Path = ".",
    scope: str | None = None,
) -> dict:
    """Rename a symbol from ``old_qname`` to its module + ``new_name``.

    ``new_name`` is the *symbol part only* (``"compute_total"``), not
    the full qname. We compute the new qname by replacing the part
    after the last ``:``.

    Returns ``{"old_qname", "new_qname", "edits_applied",
    "imports_updated", "errors", "reindex"}`` on success, or a
    structured ``{"error", "hint"}`` on top-level failure.
    """
    if scope is not None:
        return {
            "error": "scope_unsupported",
            "hint": "rename_symbol does not support vendor scopes.",
        }
    if ":" in new_name or not new_name.strip():
        return {
            "error": "invalid_new_name",
            "hint": f"new_name must be a bare identifier (got {new_name!r}).",
        }
    new_name = new_name.strip()

    root_path = Path(root).resolve()
    idx = open_index(root_path, scope=None)
    try:
        canonical, paraphrase_hint = resolve_qname(idx, old_qname)
        if canonical is None:
            return {
                "error": "not_found",
                "hint": f"No symbol {old_qname!r} in index.",
            }
        module, _, old_name = canonical.partition(":")
        new_qname = f"{module}:{new_name}"
        if old_name == new_name:
            return {
                "error": "no_op",
                "hint": f"old name and new name both {old_name!r}.",
            }
        if idx.get_symbol(new_qname) is not None:
            return {
                "error": "collision",
                "hint": (
                    f"A symbol named {new_qname!r} already exists. Pick a "
                    "different new_name or remove the existing symbol first."
                ),
            }

        def_row = idx.get_symbol(canonical)
        def_file = def_row["file"]

        # Body edits: every symbol whose body mentions the old name.
        # The ``calls`` table records ``callee_name`` even when callee
        # resolution failed, so it's the broad-net source of truth.
        # We also include the def itself.
        caller_rows = idx.conn.execute(
            "SELECT DISTINCT caller_qname FROM calls "
            "WHERE callee_name = ? AND caller_qname IS NOT NULL",
            (old_name,),
        ).fetchall()
        affected_qnames = {canonical}
        for r in caller_rows:
            affected_qnames.add(r["caller_qname"])

        # Build the batch by reading each affected symbol's source and
        # word-boundary-substituting old_name → new_name. We have to
        # read source ourselves rather than using get_source() (avoids
        # a public-API circular import from the same package).
        edits: list[dict] = []
        skipped: list[dict] = []
        for q in sorted(affected_qnames):
            row = idx.get_symbol(q)
            if row is None:
                skipped.append({"qname": q, "reason": "row_missing"})
                continue
            try:
                file_text = Path(row["file"]).read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                skipped.append({"qname": q, "reason": f"read_failed: {e}"})
                continue
            lines = file_text.splitlines()
            ls, le = row["line_start"], row["line_end"]
            body_text = "\n".join(lines[ls - 1:le])
            new_body = re.sub(
                rf"\b{re.escape(old_name)}\b",
                new_name,
                body_text,
            )
            if new_body == body_text:
                # Caller mention may have been in a string literal or a
                # comment that didn't match the word-boundary regex.
                skipped.append({"qname": q, "reason": "no_substitution"})
                continue
            edits.append({"qname": q, "new_body": new_body + "\n"})

        # Import-line rewrites. Find every ``from x import old_name``
        # or ``import old_name`` row in the imports table that points
        # at THIS def's module (avoids over-matching unrelated symbols
        # with the same short name elsewhere in the project).
        # The ``imports.module`` field is the dotted/slashed module
        # path the file imported from; ``imports.name`` is the
        # imported symbol name (NULL for plain ``import x``). We
        # approximate "imports point at this def's file" by checking
        # whether the imported module's last segment matches the def
        # file's stem — coarse but good enough for v1.
        import_rows = idx.conn.execute(
            "SELECT file, line, module, name, alias FROM imports "
            "WHERE name = ? OR (name IS NULL AND module LIKE ?)",
            (old_name, f"%{old_name}"),
        ).fetchall()
        # Filter by def's module suffix.
        def_stem = Path(def_file).stem
        candidate_imports = [
            r for r in import_rows
            if (
                # ``from x.y.def_stem import old_name``
                (r["name"] == old_name and (
                    r["module"] == def_stem
                    or r["module"].endswith(f".{def_stem}")
                    or r["module"].endswith(f"/{def_stem}")
                    or r["module"] == ""
                ))
                # ``import def_stem.old_name`` (rare)
                or (
                    r["name"] is None
                    and (r["module"] == old_name
                         or r["module"].endswith(f".{old_name}"))
                )
            )
            and r["file"] != def_file  # the def file's own ``def`` line is handled by the body edit
        ]
    finally:
        idx.close()

    # Apply body edits via the batch.
    batch_result = edit_symbol_batch(edits, root=root_path) if edits else {
        "applied": [], "errors": [], "files_touched": 0,
    }

    # Apply import-line rewrites: read each affected file once, do
    # word-boundary substitution on the named lines, write back.
    imports_updated: list[dict] = []
    import_errors: list[dict] = []
    by_file: dict[str, list[int]] = {}
    for r in candidate_imports:
        by_file.setdefault(r["file"], []).append(r["line"])

    for file_str, line_nums in by_file.items():
        path = Path(file_str)
        try:
            data = path.read_bytes()
        except OSError as e:
            import_errors.append({"file": file_str, "error": f"read_failed: {e}"})
            continue
        text = data.decode("utf-8", errors="replace")
        had_trailing_nl = text.endswith("\n")
        lines = text.split("\n")
        if had_trailing_nl:
            lines.pop()

        changed = False
        for n in line_nums:
            if n < 1 or n > len(lines):
                continue
            old_line = lines[n - 1]
            new_line = re.sub(
                rf"\b{re.escape(old_name)}\b",
                new_name,
                old_line,
            )
            if new_line != old_line:
                lines[n - 1] = new_line
                imports_updated.append({
                    "file": file_str,
                    "line": n,
                    "before": old_line,
                    "after": new_line,
                })
                changed = True

        if changed:
            new_text = "\n".join(lines)
            if had_trailing_nl:
                new_text += "\n"
            try:
                path.write_text(new_text, encoding="utf-8")
            except OSError as e:
                import_errors.append({"file": file_str, "error": f"write_failed: {e}"})

    # Final reindex picks up the import changes (the batch already
    # reindexed for the body edits, but a second pass is cheap and
    # ensures the imports table is fresh).
    from snapctx.api._indexer import index_root
    refresh = index_root(root_path)

    return {
        "old_qname": canonical,
        "new_qname": new_qname,
        "edits_applied": batch_result["applied"],
        "edit_errors": batch_result["errors"] + import_errors,
        "edits_skipped": skipped,
        "imports_updated": imports_updated,
        "files_touched": batch_result["files_touched"] + len(by_file),
        "reindex": {
            "files_updated": refresh["files_updated"],
            "files_removed": refresh["files_removed"],
        },
    }
