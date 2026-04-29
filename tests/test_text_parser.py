"""HTML / template-language / plain-text parser."""

from __future__ import annotations

from pathlib import Path

from snapctx.parsers.text import HTMLParser, TextParser


# ---------- HTML / Jinja ----------


def test_module_docstring_captures_prose_through_template_directives(tmp_path: Path) -> None:
    """The whole point of this parser: a Jinja-templated prompt file's prose
    must be searchable via embeddings, which means it must land in the module
    symbol's docstring with HTML/template noise stripped."""
    f = tmp_path / "instructions.html"
    f.write_text(
        "{% load i18n %}\n"
        "<p>You are a highly skilled biblical translator.</p>\n"
        "<p>Translate the following: {{ original_text }}</p>\n"
        "<h2>Translation Guidelines</h2>\n"
        "<ul>\n"
        "  <li>Preserve meaning, not literal word-for-word.</li>\n"
        "</ul>\n"
    )
    result = HTMLParser().parse(f, tmp_path)
    mod = next(s for s in result.symbols if s.qname == "instructions.html:")
    assert mod.docstring is not None
    assert "biblical translator" in mod.docstring.lower()
    assert "translation guidelines" in mod.docstring.lower()
    assert "preserve meaning" in mod.docstring.lower()
    # Template directives must NOT leak into the prose.
    assert "{%" not in mod.docstring
    assert "{{" not in mod.docstring
    # HTML tags must NOT leak either.
    assert "<p>" not in mod.docstring
    assert "<h2>" not in mod.docstring


def test_h1_through_h6_become_nested_symbols(tmp_path: Path) -> None:
    f = tmp_path / "page.html"
    f.write_text(
        "<h1>Project</h1>\n"
        "<h2>Setup</h2>\n"
        "<p>install steps</p>\n"
        "<h2>Usage</h2>\n"
        "<h3>Examples</h3>\n"
    )
    qnames = {s.qname for s in HTMLParser().parse(f, tmp_path).symbols}
    assert "page.html:" in qnames
    assert "page.html:Project" in qnames
    assert "page.html:Project.Setup" in qnames
    assert "page.html:Project.Usage" in qnames
    assert "page.html:Project.Usage.Examples" in qnames


def test_title_tag_becomes_a_symbol(tmp_path: Path) -> None:
    f = tmp_path / "page.html"
    f.write_text("<html><head><title>My Page</title></head><body>x</body></html>")
    qnames = {s.qname for s in HTMLParser().parse(f, tmp_path).symbols}
    assert "page.html:My Page" in qnames


def test_html_entities_are_decoded_in_docstring(tmp_path: Path) -> None:
    f = tmp_path / "x.html"
    f.write_text("<p>Use &amp; carefully &mdash; AT&amp;T isn&#39;t typical.</p>")
    mod = next(s for s in HTMLParser().parse(f, tmp_path).symbols if s.qname == "x.html:")
    assert mod.docstring is not None
    assert "&amp;" not in mod.docstring
    assert "Use & carefully" in mod.docstring
    assert "isn't" in mod.docstring


def test_jinja_extension_is_handled(tmp_path: Path) -> None:
    """``.j2`` / ``.jinja`` / ``.jinja2`` files use the same parser."""
    for ext in (".j2", ".jinja", ".jinja2"):
        f = tmp_path / f"prompt{ext}"
        f.write_text("System: You are a helpful assistant.\nUser: {{ query }}\n")
        result = HTMLParser().parse(f, tmp_path)
        mod = result.symbols[0]
        assert mod.docstring is not None
        assert "helpful assistant" in mod.docstring


def test_handlebars_braces_are_stripped(tmp_path: Path) -> None:
    f = tmp_path / "email.hbs"
    f.write_text("Hello {{name}}, your order #{{order_id}} has shipped.\n")
    mod = HTMLParser().parse(f, tmp_path).symbols[0]
    assert mod.docstring is not None
    assert "{{" not in mod.docstring
    assert "Hello" in mod.docstring
    assert "your order" in mod.docstring


def test_long_prose_is_truncated_with_ellipsis(tmp_path: Path) -> None:
    """The docstring caps at ~480 chars — the embedding model truncates at
    512 tokens regardless, so longer prose just wastes batch padding."""
    body = "First sentence. " + ("filler word " * 200)
    f = tmp_path / "x.html"
    f.write_text(f"<p>{body}</p>")
    mod = HTMLParser().parse(f, tmp_path).symbols[0]
    assert mod.docstring is not None
    assert len(mod.docstring) <= 481
    assert mod.docstring.startswith("First sentence")
    assert mod.docstring.endswith("…")


def test_empty_html_returns_module_symbol_with_no_docstring(tmp_path: Path) -> None:
    f = tmp_path / "blank.html"
    f.write_text("<!DOCTYPE html>\n<html><head></head><body></body></html>\n")
    result = HTMLParser().parse(f, tmp_path)
    mod = next(s for s in result.symbols if s.qname == "blank.html:")
    assert mod.docstring is None or mod.docstring == ""


def test_empty_headings_are_skipped(tmp_path: Path) -> None:
    """An ``<h1></h1>`` with nothing inside shouldn't create a symbol."""
    f = tmp_path / "x.html"
    f.write_text("<h1></h1><h2>Real</h2>")
    qnames = {s.qname for s in HTMLParser().parse(f, tmp_path).symbols}
    assert "x.html:Real" in qnames
    # No empty-name heading.
    assert all(q.split(":")[-1] in {"", "Real"} for q in qnames)


# ---------- plain text ----------


def test_text_file_module_docstring_captures_prose(tmp_path: Path) -> None:
    f = tmp_path / "system_prompt.txt"
    f.write_text("You are a helpful assistant.\nFollow the user's instructions carefully.\n")
    result = TextParser().parse(f, tmp_path)
    assert len(result.symbols) == 1
    mod = result.symbols[0]
    assert mod.qname == "system_prompt.txt:"
    assert mod.docstring is not None
    assert "helpful assistant" in mod.docstring
    assert "instructions carefully" in mod.docstring


def test_text_parser_emits_no_calls_or_imports(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("hello\n")
    result = TextParser().parse(f, tmp_path)
    assert result.calls == []
    assert result.imports == []


# ---------- registry wiring ----------


def test_registry_routes_html_extensions(tmp_path: Path) -> None:
    from snapctx.parsers.registry import parser_for, parser_for_path

    assert parser_for(".html").__class__.__name__ == "HTMLParser"
    assert parser_for(".j2").__class__.__name__ == "HTMLParser"
    assert parser_for(".liquid").__class__.__name__ == "HTMLParser"
    assert parser_for(".hbs").__class__.__name__ == "HTMLParser"
    assert parser_for(".txt").__class__.__name__ == "TextParser"

    p = tmp_path / "page.html"
    p.write_text("<p>x</p>")
    assert parser_for_path(p).__class__.__name__ == "HTMLParser"
