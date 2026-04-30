"""Compact, multi-root project skeleton sized for an LLM cached preamble.

``map_repo`` returns a JSON payload designed for an agent's tool to
consume; for an LLM **system prompt** that the Anthropic prompt cache
will hold across a long coding session, JSON is the wrong shape:

* JSON is verbose — keys repeat every entry.
* The cached preamble has to fit in a budget (target: ≤ 8 KB for a
  typical 200-file repo) so subsequent turns hit cached input rather
  than re-tokenizing.
* Multi-root monorepos (backend + frontend) want one call, one block,
  with each entry tagged by which root it came from.

``session_skeleton`` is the text-rendering counterpart to
``map_repo``: open each root's index once, walk top-level symbols,
emit a directory + file + per-symbol line per entry. Output is plain
markdown-ish text — no JSON wrapper, no ``token_estimate`` field, no
file-level ``hint``.

Two render modes:

* ``"compact"`` (default) — directory header, file path with one-line
  module-docstring summary, then one ``[kind] qname  signature`` per
  top-level symbol. Decorators inlined when present (they're often
  the most identifying fact: ``@app.route('/login')``,
  ``@dataclass(frozen=True)``).
* ``"minimal"`` — directory + file + qnames only (no signatures, no
  summaries, no decorators). For very large projects to fit in cache
  budget. Roughly half the size of compact.

The output is truncated past ``max_chars`` (default 8 KB) with a
trailing ``# ... (truncated)`` line so the LLM knows it's clipped.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Literal

from snapctx.api._common import docstring_summary, open_index


SkeletonRender = Literal["compact", "minimal"]

# Soft cap. A 200-file mid-sized monorepo ought to fit in this much,
# and Anthropic's ephemeral cache has a minimum cacheable block size
# (~1 KB) and works best when the full block is well under prompt
# context — 8 KB is a reasonable head room.
DEFAULT_MAX_CHARS = 8000


def session_skeleton(
    roots: list[Path] | list[str] | Path | str,
    render: SkeletonRender = "compact",
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    anchor: Path | str | None = None,
) -> str:
    """Render a multi-root project skeleton as compact text.

    ``roots`` accepts either a single path or a list — a single Path is
    promoted internally so callers don't need a wrapper for the
    one-root case.

    Each root's directories are emitted in source-tree order. Files
    appear under their directory, with their module docstring summary
    (when ``render="compact"``) and their top-level symbols
    (always — minimal still lists qnames; that's the only way the
    LLM knows what's there to call ``get_source`` on).

    When ``len(roots) > 1``, each directory header is prefixed with the
    root label (e.g. ``## backend/checkout/``) so the agent can route
    correctly. When ``len(roots) == 1`` we omit the prefix to keep the
    payload small.

    ``max_chars`` (default 8 KB) is a soft cap. We accumulate output
    and stop appending when we'd exceed it, leaving a
    ``# ... (truncated, N more files) ...`` marker so the LLM knows
    we clipped.
    """
    if render not in ("compact", "minimal"):
        raise ValueError(
            f"render must be 'compact' or 'minimal', got {render!r}"
        )

    norm_roots: list[Path]
    if isinstance(roots, (str, Path)):
        norm_roots = [Path(roots).resolve()]
    else:
        norm_roots = [Path(r).resolve() for r in roots]
    if not norm_roots:
        return ""

    anchor_path: Path | None
    if anchor is not None:
        anchor_path = Path(anchor).resolve()
    elif len(norm_roots) > 1:
        # Try the common parent so labels stay short; falls back to
        # absolute paths if there's no shared prefix.
        try:
            anchor_path = Path(
                _common_parent([str(r) for r in norm_roots])
            )
        except ValueError:
            anchor_path = None
    else:
        anchor_path = None

    multi = len(norm_roots) > 1
    out: list[str] = []
    used = 0
    truncated = False
    skipped_files = 0

    for root in norm_roots:
        label = _root_label(root, anchor_path) if multi else None
        try:
            files = _collect_top_level(root)
        except FileNotFoundError:
            files = []
            # Surface the missing index inline so the agent knows the
            # root is opaque rather than empty.
            line = f"# (no snapctx index at {root}; run `snapctx index`)"
            out.append(line)
            used += len(line) + 1
            continue
        if not files:
            continue

        # Group files by their directory (root-relative).
        by_dir: dict[str, list[dict]] = defaultdict(list)
        for entry in files:
            by_dir[entry["dir"]].append(entry)

        for d in sorted(by_dir):
            header = _dir_header(d, label)
            file_block_lines: list[str] = [header]
            for f in by_dir[d]:
                file_block_lines.extend(_render_file(f, render))
            file_block_lines.append("")  # blank line between directories
            block = "\n".join(file_block_lines) + "\n"

            if used + len(block) > max_chars:
                truncated = True
                # Count how many more files we'd skip past this block.
                skipped_files += sum(
                    len(by_dir[od]) for od in by_dir if od >= d
                )
                break
            out.append(block)
            used += len(block)
        if truncated:
            break

    if truncated:
        out.append(
            f"# ... (truncated to {max_chars} chars; "
            f"~{skipped_files} more files in the repo) ...\n"
        )

    return "".join(out).rstrip() + "\n"


def _collect_top_level(root: Path) -> list[dict]:
    """Pull every file's top-level symbols + module docstring out of one index.

    Returns a list of ``{file (rel), dir (rel), summary, symbols}``
    dicts in source-tree order. ``summary`` is the file's module
    docstring summary (``None`` if there isn't one). ``symbols`` is
    a list of dicts ready for ``_render_file``.
    """
    idx = open_index(root, scope=None)
    try:
        rows = idx.conn.execute(
            "SELECT * FROM symbols "
            "WHERE parent_qname IS NULL "
            "ORDER BY file ASC, line_start ASC"
        ).fetchall()
    finally:
        idx.close()

    by_file: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        by_file[row["file"]].append(row)

    out: list[dict] = []
    for file_str, file_rows in by_file.items():
        rel = _relative(file_str, root)
        # Hoist module docstring to a file-level summary; drop the
        # synthetic ``module:`` symbol.
        summary: str | None = None
        symbols: list[dict] = []
        for r in file_rows:
            if r["kind"] == "module":
                if summary is None:
                    summary = docstring_summary(r["docstring"])
                continue
            symbols.append({
                "qname": r["qname"],
                "kind": r["kind"],
                "signature": r["signature"] or "",
                "decorators": (
                    r["decorators"].split("\n") if r["decorators"] else []
                ),
                "docstring": docstring_summary(r["docstring"]),
            })
        if not symbols and not summary:
            continue
        out.append({
            "file": rel,
            "dir": str(Path(rel).parent),
            "summary": summary,
            "symbols": symbols,
        })
    return out


def _render_file(entry: dict, render: SkeletonRender) -> list[str]:
    """Format one file's block as a list of text lines (no trailing newline)."""
    lines: list[str] = []
    if render == "compact" and entry["summary"]:
        lines.append(f"- {entry['file']}  — {entry['summary']}")
    else:
        lines.append(f"- {entry['file']}")
    for s in entry["symbols"]:
        if render == "minimal":
            lines.append(f"    {s['qname']}")
            continue
        # Compact: [kind] qname signature, with decorators inline when present.
        sig = s["signature"]
        line = f"    [{s['kind']}] {s['qname']}"
        if sig:
            line += f"  {sig}"
        if s["decorators"]:
            line += "  " + " ".join(s["decorators"])
        lines.append(line)
    return lines


def _dir_header(d: str, label: str | None) -> str:
    """Format a directory header. For multi-root, prefix with the root label."""
    if label:
        return f"## {label}/{d}/" if d != "." else f"## {label}/"
    return f"## {d}/" if d != "." else "## ./"


def _relative(file_str: str, root: Path) -> str:
    """Path relative to root — keeps the skeleton readable."""
    try:
        return str(Path(file_str).resolve().relative_to(root))
    except ValueError:
        return file_str


def _root_label(root: Path, anchor: Path | None) -> str:
    """Short label for a root in a multi-root rendering.

    With an anchor (typical: the workspace parent), use the
    anchor-relative path so labels stay short. Without one, fall back
    to the directory's basename.
    """
    if anchor is not None:
        try:
            return str(root.relative_to(anchor))
        except ValueError:
            pass
    return root.name


def _common_parent(paths: list[str]) -> str:
    """Longest common parent directory of ``paths``.

    Raises ``ValueError`` when they share no parent (e.g., on Windows
    cross-drive). We only use this to make multi-root labels shorter,
    so falling back to absolute paths is fine.
    """
    if not paths:
        raise ValueError("no paths")
    common = Path(paths[0]).parent
    for p in paths[1:]:
        pp = Path(p)
        # Walk up until ``common`` is a parent of ``pp``.
        while True:
            try:
                pp.relative_to(common)
                break
            except ValueError:
                if common.parent == common:
                    raise ValueError("no common parent")
                common = common.parent
    return str(common)
