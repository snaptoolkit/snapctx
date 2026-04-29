"""HTML / template-language / plain-text parser.

For prompt templates, email templates, system-message files, content
pages, and instruction text — places where the *answer* to a code
question often lives but neither Python nor TypeScript parsing can see.

We don't model HTML structure. We strip HTML tags and template
directives (Jinja ``{% %}`` / ``{{ }}`` / ``{# #}``, also valid for
Liquid / Twig / Nunjucks; Handlebars ``{{ }}`` is covered by the same
brace-pair pattern), keep the resulting prose, and embed the first
~480 characters as the module symbol's docstring. That prose is what
goes through both the FTS5 index and the embedding model — so a query
like ``"translation instructions"`` matches the actual instruction
text in ``instructions_translation_from_original.html`` instead of
matching only the Python function that loads it.

We do emit additional symbols for ``<title>`` and ``<h1>``..``<h6>``
so a templates dir's section structure shows up in
``snapctx_outline``. Headings own only their own line — HTML's
heading semantics aren't strict enough to compute reliable body
extents the way Markdown's are, and incorrect bodies would just add
noise to embeddings.

We don't emit calls or imports.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from snapctx.qname import make_qname
from snapctx.schema import ParseResult, Symbol


# HTML tags, comments, CDATA, doctype. Non-greedy across newlines.
_HTML_TAG_RE = re.compile(
    r"<!--.*?-->|<!\[CDATA\[.*?\]\]>|<![^>]*>|<[^>]+>", re.DOTALL
)
# Template directives — covers Jinja / Liquid / Twig / Nunjucks (``{% %}``,
# ``{{ }}``, ``{# #}``) and Handlebars / Mustache (``{{ }}``). Same brace-pair
# shape, deliberately broad — the goal is *strip*, not *parse*.
_TEMPLATE_TAG_RE = re.compile(r"\{[%{#].*?[#}%]\}", re.DOTALL)
_HEADING_RE = re.compile(r"<h([1-6])\b[^>]*>(.*?)</h\1>", re.DOTALL | re.IGNORECASE)
_TITLE_RE = re.compile(r"<title\b[^>]*>(.*?)</title>", re.DOTALL | re.IGNORECASE)
_WS_RE = re.compile(r"\s+")
_ENTITY_REPLACEMENTS = (
    ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
    ("&quot;", '"'), ("&#39;", "'"), ("&apos;", "'"), ("&nbsp;", " "),
)

# Embedding text caps at 512 chars (see embeddings.symbol_text_for_embedding).
# 480 leaves headroom so the qname/signature aren't clipped.
_DOCSTRING_LIMIT = 480


def _strip_template_and_html(source: str) -> str:
    """Strip template directives and HTML tags, leaving prose."""
    s = _TEMPLATE_TAG_RE.sub(" ", source)
    return _HTML_TAG_RE.sub(" ", s)


def _decode_entities(s: str) -> str:
    for src, dst in _ENTITY_REPLACEMENTS:
        s = s.replace(src, dst)
    return s


def _summarize(prose: str) -> str | None:
    """First chunk of meaningful prose, capped at ``_DOCSTRING_LIMIT``."""
    flat = _decode_entities(_WS_RE.sub(" ", prose)).strip()
    if not flat:
        return None
    if len(flat) <= _DOCSTRING_LIMIT:
        return flat
    cut = flat.rfind(" ", 0, _DOCSTRING_LIMIT)
    head = flat[:cut] if cut > _DOCSTRING_LIMIT // 2 else flat[:_DOCSTRING_LIMIT]
    return head.rstrip() + "…"


def _line_of(source: str, offset: int) -> int:
    return source.count("\n", 0, offset) + 1


class HTMLParser:
    """Parser entry point for HTML and template-language files."""

    language = "html"
    # Twig is sometimes ``.html.twig`` — Path.suffix returns ``.twig`` only,
    # so listing ``.twig`` covers both. Same for ``.html.j2`` → ``.j2``.
    extensions = (
        ".html", ".htm",
        ".j2", ".jinja", ".jinja2",
        ".liquid", ".njk", ".twig",
        ".hbs", ".handlebars", ".mustache",
    )

    def parse(self, path: Path, root: Path) -> ParseResult:
        source = path.read_text(encoding="utf-8", errors="replace")
        rel = path.resolve().relative_to(root.resolve())
        module = "/".join(rel.parts)
        file_str = str(path.resolve())
        line_count = max(1, source.count("\n") + 1)

        prose = _strip_template_and_html(source)
        symbols: list[Symbol] = []
        module_qname = make_qname(module, [])
        symbols.append(Symbol(
            qname=module_qname,
            kind="module",
            language=self.language,
            signature=f"html {module}",
            docstring=_summarize(prose),
            file=file_str,
            line_start=1,
            line_end=line_count,
            parent_qname=None,
            source_sha=_sha(source),
        ))

        title = _TITLE_RE.search(source)
        if title:
            name = _decode_entities(_WS_RE.sub(" ", _strip_template_and_html(title.group(1)))).strip()
            if name:
                line_no = _line_of(source, title.start())
                symbols.append(Symbol(
                    qname=make_qname(module, [name]),
                    kind="module",
                    language=self.language,
                    signature=f"<title> {name}",
                    docstring=name,
                    file=file_str,
                    line_start=line_no,
                    line_end=line_no,
                    parent_qname=module_qname,
                    source_sha=_sha(name),
                ))

        path_stack: list[tuple[int, str]] = []
        for m in _HEADING_RE.finditer(source):
            level = int(m.group(1))
            name = _decode_entities(
                _WS_RE.sub(" ", _strip_template_and_html(m.group(2)))
            ).strip()
            if not name:
                continue
            line_no = _line_of(source, m.start())
            while path_stack and path_stack[-1][0] >= level:
                path_stack.pop()
            path_stack.append((level, name))
            qname = make_qname(module, [n for _, n in path_stack])
            parent_qname = (
                make_qname(module, [n for _, n in path_stack[:-1]])
                if len(path_stack) > 1 else module_qname
            )
            symbols.append(Symbol(
                qname=qname,
                kind="module",
                language=self.language,
                signature=f"<h{level}> {name}",
                docstring=name,
                file=file_str,
                line_start=line_no,
                line_end=line_no,
                parent_qname=parent_qname,
                source_sha=_sha(name),
            ))

        return ParseResult(symbols=symbols, calls=[], imports=[], language=self.language)


class TextParser:
    """Plain-text parser. One module symbol per file with a prose docstring."""

    language = "text"
    extensions = (".txt",)

    def parse(self, path: Path, root: Path) -> ParseResult:
        source = path.read_text(encoding="utf-8", errors="replace")
        rel = path.resolve().relative_to(root.resolve())
        module = "/".join(rel.parts)
        line_count = max(1, source.count("\n") + 1)
        symbol = Symbol(
            qname=make_qname(module, []),
            kind="module",
            language=self.language,
            signature=f"text {module}",
            docstring=_summarize(source),
            file=str(path.resolve()),
            line_start=1,
            line_end=line_count,
            parent_qname=None,
            source_sha=_sha(source),
        )
        return ParseResult(symbols=[symbol], calls=[], imports=[], language=self.language)


def _sha(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()
