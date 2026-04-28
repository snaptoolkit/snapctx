"""Markdown parser — headings become indexed symbols.

A heading like ``## Tool benchmark`` becomes a ``module``-kind symbol
with qname ``path/to/file.md:Tool benchmark``. Nested headings build
a dotted path (``# Top`` → ``## Sub`` → qname ``…:Top.Sub``) so a
file's section structure shows up in ``snapctx_outline`` and
``snapctx_search``.

Body extents follow Markdown-natural rules: a heading owns every line
from itself up to the next heading at the same level or shallower
(or end-of-file). The heading's text becomes both the signature and
the docstring (1-line summary). Code-fenced blocks (``` ``` ```/
``~~~``) are skipped so a ``# inside code`` doesn't get parsed as a
heading.

We deliberately don't emit calls or imports — markdown has neither.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from snapctx.qname import make_qname
from snapctx.schema import ParseResult, Symbol


_ATX_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")


class MarkdownParser:
    """Parser entry point. Implements ``parsers.base.Parser`` protocol."""

    language = "markdown"
    extensions = (".md", ".markdown")

    def parse(self, path: Path, root: Path) -> ParseResult:
        source = path.read_text(encoding="utf-8", errors="replace")
        rel = path.resolve().relative_to(root.resolve())
        module = "/".join(rel.parts)  # full path is the module identity for md
        file_str = str(path.resolve())

        lines = source.splitlines()
        line_count = max(1, len(lines))
        headings = list(_iter_headings(lines))

        symbols: list[Symbol] = []

        # Module symbol — the file as a whole. Docstring = first
        # non-blank, non-heading paragraph (a typical README intro).
        module_qname = make_qname(module, [])
        symbols.append(Symbol(
            qname=module_qname,
            kind="module",
            language=self.language,
            signature=f"markdown {module}",
            docstring=_leading_paragraph(lines, headings),
            file=file_str,
            line_start=1,
            line_end=line_count,
            parent_qname=None,
            source_sha=_sha(source),
        ))

        # Heading symbols. Each heading owns lines [its_line .. just_before_next_same_or_higher].
        path_stack: list[tuple[int, str]] = []  # [(level, name), …]
        for idx, (level, name, line_no) in enumerate(headings):
            while path_stack and path_stack[-1][0] >= level:
                path_stack.pop()
            path_stack.append((level, name))

            end_line = line_count
            for j in range(idx + 1, len(headings)):
                jl, _, jline = headings[j]
                if jl <= level:
                    end_line = jline - 1
                    break

            qname = make_qname(module, [n for _, n in path_stack])
            parent_qname = (
                make_qname(module, [n for _, n in path_stack[:-1]])
                if len(path_stack) > 1
                else module_qname
            )
            body = "\n".join(lines[line_no - 1 : end_line])
            symbols.append(Symbol(
                qname=qname,
                kind="module",  # reuse 'module' — no dedicated heading kind in schema
                language=self.language,
                signature=f"{'#' * level} {name}",
                docstring=name,
                file=file_str,
                line_start=line_no,
                line_end=end_line,
                parent_qname=parent_qname,
                source_sha=_sha(body),
            ))

        return ParseResult(symbols=symbols, calls=[], imports=[], language=self.language)


def _iter_headings(lines: list[str]):
    """Yield ``(level, name, line_no)`` for every ATX heading outside code fences."""
    in_fence = False
    fence_marker = ""
    for i, line in enumerate(lines, start=1):
        stripped = line.lstrip()
        # Toggle on ```/~~~ fences (must be at start of line, ignoring leading ws).
        if stripped.startswith(("```", "~~~")):
            marker = stripped[:3]
            if not in_fence:
                in_fence = True
                fence_marker = marker
            elif marker == fence_marker:
                in_fence = False
                fence_marker = ""
            continue
        if in_fence:
            continue
        m = _ATX_RE.match(line)
        if not m:
            continue
        level = len(m.group(1))
        name = m.group(2).strip()
        if not name:
            continue
        yield level, name, i


def _leading_paragraph(lines: list[str], headings) -> str | None:
    """First non-blank, non-heading paragraph — used as the module docstring."""
    heading_lines = {ln for _, _, ln in headings}
    out: list[str] = []
    for i, line in enumerate(lines, start=1):
        if i in heading_lines:
            if out:
                break
            continue
        s = line.strip()
        if not s:
            if out:
                break
            continue
        if s.startswith(("```", "~~~")):
            break
        out.append(s)
        if len(out) >= 3:
            break
    return " ".join(out) if out else None


def _sha(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()
