"""``grep`` — literal/regex search over EVERY text file under the root.

Closes the structural gap between ``find`` and the agent's external
``grep``. ``find`` is symbol-body-only — it can't see comments above
the first ``def``, content inside ``README.md``, keys inside
``settings.toml``, etc. ``grep`` walks the same gitignore / vendor /
size rules the indexer uses but yields **all** text-like files, so an
agent never has to reach back to a generic shell ``grep`` to find a
URL, env-var name, TODO marker, or markdown heading.

Hits are annotated with the enclosing-symbol qname when they land
inside an indexed symbol's line range, so a grep result on a code
file routes the agent straight to ``snapctx_source <qname>`` without
a follow-up ``snapctx_search``.

Returns ``{pattern, regex, matches, match_count, files_scanned,
truncated, hint}``. Each match is ``{file, line, text, before,
after, qname?}`` where ``before``/``after`` are 0–N lines of context
and ``qname`` is present only when the line falls inside an indexed
symbol.
"""

from __future__ import annotations

import re
from pathlib import Path

from snapctx.api._common import open_index
from snapctx.config import load_config
from snapctx.walker import iter_text_files


def grep_files(
    pattern: str,
    root: str | Path = ".",
    scope: str | None = None,
    *,
    regex: bool = False,
    in_path: str | None = None,
    case_insensitive: bool = False,
    context_lines: int = 1,
    max_results: int = 200,
    max_files: int = 5000,
) -> dict:
    """Search every text file under ``root`` for ``pattern``.

    ``regex`` toggles between literal substring (default — fast,
    grep -F semantics) and Python regex. ``in_path`` narrows the walk
    to files whose path starts with that prefix (relative to root).
    ``case_insensitive`` is honored in both modes. ``context_lines``
    is the number of leading/trailing lines around each match.
    ``max_results`` caps total hits; ``max_files`` caps files scanned
    (early-exit on huge trees).
    """
    if not pattern:
        return {
            "pattern": pattern,
            "regex": regex,
            "matches": [],
            "match_count": 0,
            "files_scanned": 0,
            "truncated": False,
            "hint": "Pass a non-empty pattern.",
        }
    if scope is not None:
        return {
            "error": "scope_unsupported",
            "hint": "grep does not support vendor scopes; vendor packages are code-only.",
        }

    root_path = Path(root).resolve()
    cfg = load_config(root_path)

    if regex:
        flags = re.IGNORECASE if case_insensitive else 0
        try:
            compiled = re.compile(pattern, flags)
        except re.error as e:
            return {
                "error": "invalid_regex",
                "hint": f"Could not compile pattern: {e}",
            }
        matcher = compiled.search
    else:
        if case_insensitive:
            needle = pattern.lower()
            matcher = (lambda line, n=needle: n in line.lower())
        else:
            matcher = (lambda line, n=pattern: n in line)

    in_prefix: str | None = None
    if in_path:
        p = Path(in_path)
        if p.is_absolute():
            try:
                in_prefix = str(p.resolve().relative_to(root_path))
            except ValueError:
                in_prefix = None  # outside root → no files match
        else:
            in_prefix = str(p)

    matches: list[dict] = []
    files_scanned = 0
    truncated = False

    symbol_index_by_file = _load_symbol_ranges(root_path)

    for path in iter_text_files(root_path, cfg.walker):
        if files_scanned >= max_files:
            truncated = True
            break
        rel_str = str(path.relative_to(root_path))
        if in_prefix is not None and not _under_prefix(rel_str, in_prefix):
            continue
        files_scanned += 1
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lines = text.splitlines()
        file_str = str(path)
        ranges = symbol_index_by_file.get(file_str, [])
        for i, line in enumerate(lines, start=1):
            if not matcher(line):
                continue
            hit: dict = {
                "file": file_str,
                "line": i,
                "text": line.rstrip(),
            }
            if context_lines > 0:
                lo = max(0, i - 1 - context_lines)
                hi = min(len(lines), i + context_lines)
                hit["before"] = [ln.rstrip() for ln in lines[lo : i - 1]]
                hit["after"] = [ln.rstrip() for ln in lines[i:hi]]
            qname = _enclosing_qname(ranges, i)
            if qname:
                hit["qname"] = qname
            matches.append(hit)
            if len(matches) >= max_results:
                truncated = True
                break
        if truncated:
            break

    return {
        "pattern": pattern,
        "regex": regex,
        "matches": matches,
        "match_count": len(matches),
        "files_scanned": files_scanned,
        "truncated": truncated,
        "hint": _hint_for(matches, truncated, max_results, regex),
    }


def _under_prefix(rel_path: str, prefix: str) -> bool:
    """True iff ``rel_path`` is the prefix path or a descendant of it."""
    p = prefix.rstrip("/")
    return rel_path == p or rel_path.startswith(p + "/")


def _load_symbol_ranges(root: Path) -> dict[str, list[tuple[int, int, str]]]:
    """Map ``file -> [(line_start, line_end, qname), …]`` for qname annotation.

    Returns an empty mapping if no index exists; ``grep`` still works,
    just without qname tagging.
    """
    try:
        idx = open_index(root, scope=None)
    except Exception:
        return {}
    try:
        rows = idx.conn.execute(
            "SELECT file, line_start, line_end, qname FROM symbols",
        ).fetchall()
    except Exception:
        idx.close()
        return {}
    idx.close()
    by_file: dict[str, list[tuple[int, int, str]]] = {}
    for r in rows:
        by_file.setdefault(r["file"], []).append(
            (int(r["line_start"]), int(r["line_end"]), r["qname"])
        )
    for v in by_file.values():
        v.sort(key=lambda t: t[1] - t[0])  # tightest range first → innermost wins
    return by_file


def _enclosing_qname(
    ranges: list[tuple[int, int, str]], line: int
) -> str | None:
    """Return the qname of the smallest symbol whose range contains ``line``."""
    for ls, le, q in ranges:
        if ls <= line <= le:
            return q
    return None


def _hint_for(
    matches: list[dict], truncated: bool, max_results: int, regex: bool,
) -> str:
    if not matches:
        return (
            "No matches. Try toggling regex=True for pattern syntax, "
            "case_insensitive=True, or removing in_path."
        )
    if truncated:
        return (
            f"Hit cap ({max_results}). Narrow with in_path=<dir> or raise "
            "max_results."
        )
    annotated = sum(1 for m in matches if "qname" in m)
    if annotated and not regex:
        return (
            f"{len(matches)} hits ({annotated} inside indexed symbols). "
            "Call snapctx_source on a qname for the full enclosing body."
        )
    return f"{len(matches)} hits across {len({m['file'] for m in matches})} files."
