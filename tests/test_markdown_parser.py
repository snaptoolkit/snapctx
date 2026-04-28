"""Markdown parser — headings become indexed symbols."""

from __future__ import annotations

from pathlib import Path

from snapctx.parsers.markdown import MarkdownParser


def test_headings_become_symbols(tmp_path: Path) -> None:
    f = tmp_path / "doc.md"
    f.write_text(
        "# Project\n\n"
        "Intro paragraph.\n\n"
        "## Setup\n\n"
        "Run pip install.\n\n"
        "## Usage\n\n"
        "Call the API.\n"
    )
    result = MarkdownParser().parse(f, tmp_path)
    qnames = {s.qname for s in result.symbols}
    assert "doc.md:" in qnames
    assert "doc.md:Project" in qnames
    assert "doc.md:Project.Setup" in qnames
    assert "doc.md:Project.Usage" in qnames


def test_module_docstring_is_first_paragraph(tmp_path: Path) -> None:
    f = tmp_path / "r.md"
    f.write_text(
        "# Title\n\n"
        "This is the intro.\n"
        "It spans multiple lines.\n\n"
        "## Next\n"
    )
    result = MarkdownParser().parse(f, tmp_path)
    mod = next(s for s in result.symbols if s.qname == "r.md:")
    assert mod.docstring is not None
    assert "intro" in mod.docstring.lower()


def test_code_fences_skip_heading_detection(tmp_path: Path) -> None:
    """A `#` line inside a fenced code block is not a heading."""
    f = tmp_path / "x.md"
    f.write_text(
        "# Real heading\n\n"
        "```bash\n"
        "# this is a comment, not a heading\n"
        "echo hi\n"
        "```\n\n"
        "## Also real\n"
    )
    result = MarkdownParser().parse(f, tmp_path)
    qnames = {s.qname for s in result.symbols}
    assert "x.md:Real heading" in qnames
    assert "x.md:Real heading.Also real" in qnames
    assert not any("comment" in q for q in qnames)


def test_heading_line_ranges_cover_section(tmp_path: Path) -> None:
    """A section ends at the next heading at the same or shallower level.

    H1 ``# A`` includes its nested ``## B`` (B is a child); a sibling
    H1 ``# C`` would terminate A.
    """
    f = tmp_path / "s.md"
    f.write_text(
        "# A\n"
        "body of A\n"
        "## B\n"
        "body of B\n"
        "# C\n"
        "body of C\n"
    )
    result = MarkdownParser().parse(f, tmp_path)
    a = next(s for s in result.symbols if s.qname == "s.md:A")
    b = next(s for s in result.symbols if s.qname == "s.md:A.B")
    c = next(s for s in result.symbols if s.qname == "s.md:C")
    assert a.line_start == 1 and a.line_end == 4  # spans through B's body
    assert b.line_start == 3 and b.line_end == 4
    assert c.line_start == 5


def test_no_calls_or_imports(tmp_path: Path) -> None:
    f = tmp_path / "x.md"
    f.write_text("# H\n\nbody\n")
    result = MarkdownParser().parse(f, tmp_path)
    assert result.calls == []
    assert result.imports == []
