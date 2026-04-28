"""``edit_symbol``: replace a symbol body by qname.

The contract is "query first, edit second" — the staleness guard
relies on the file's indexed SHA matching the current SHA. If the
file changed since indexing, the edit refuses and asks the caller
to re-query for fresh coordinates.
"""

from __future__ import annotations

from pathlib import Path

from snapctx.api import edit_symbol, get_source, index_root


def _build_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "math.py").write_text(
        '"""Math helpers."""\n'
        "\n"
        "def add(a, b):\n"
        '    """Sum two numbers."""\n'
        "    return a + b\n"
        "\n"
        "\n"
        "def mul(a, b):\n"
        '    """Multiply two numbers."""\n'
        "    return a * b\n"
    )
    index_root(repo)
    return repo


def test_edit_replaces_function_body_and_reindex_picks_it_up(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)

    new_body = (
        "def add(a, b, c=0):\n"
        '    """Sum two or three numbers."""\n'
        "    return a + b + c\n"
    )
    result = edit_symbol("pkg.math:add", new_body, root=repo)

    assert "error" not in result, result
    assert result["qname"] == "pkg.math:add"
    assert result["lines_replaced"] == 3
    assert result["lines_written"] == 3
    assert result["reindex"]["files_updated"] == 1

    on_disk = (repo / "pkg" / "math.py").read_text()
    assert "def add(a, b, c=0):" in on_disk
    assert "return a + b + c" in on_disk
    # Untouched neighbour preserved verbatim.
    assert "def mul(a, b):" in on_disk

    # get_source after edit reflects the new body and new signature.
    src = get_source("pkg.math:add", root=repo)
    # The parser normalizes whitespace around defaults (``c = 0`` not ``c=0``);
    # we just assert the third parameter and the new body are visible.
    assert "c" in src["signature"]
    assert "return a + b + c" in src["source"]


def test_edit_unknown_qname_returns_not_found(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)

    result = edit_symbol("pkg.math:missing", "irrelevant\n", root=repo)
    assert result["error"] == "not_found"
    assert "search" in result["hint"].lower()
    # File untouched.
    assert "def add(a, b):" in (repo / "pkg" / "math.py").read_text()


def test_edit_refuses_when_file_changed_since_index(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)

    # Simulate an external editor: line numbers in the index now lie.
    target = repo / "pkg" / "math.py"
    target.write_text("# header inserted by another tool\n" + target.read_text())

    result = edit_symbol("pkg.math:add", "def add(a, b): return a + b\n", root=repo)
    assert result["error"] == "stale_coordinates"
    assert "re-query" in result["hint"].lower()
    # File still has the user's external edit untouched — we did not write.
    assert (repo / "pkg" / "math.py").read_text().startswith("# header inserted")


def test_edit_preserves_trailing_newline(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    before = (repo / "pkg" / "math.py").read_text()
    assert before.endswith("\n")

    edit_symbol(
        "pkg.math:add",
        "def add(a, b):\n    return a + b  # tweaked\n",
        root=repo,
    )
    after = (repo / "pkg" / "math.py").read_text()
    assert after.endswith("\n")
    # The PEP-8 blank-line gap between top-level functions must survive
    # the splice — neither swallowed nor doubled.
    assert "  # tweaked\n\n\ndef mul" in after


def test_edit_rejects_vendor_scope(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    result = edit_symbol("pkg.math:add", "x", root=repo, scope="django")
    assert result["error"] == "scope_unsupported"


def test_edit_refuses_python_syntax_error(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    bad = "def add(a, b\n    return a + b\n"  # missing close paren on def line
    result = edit_symbol("pkg.math:add", bad, root=repo)
    assert result["error"] == "syntax_error"
    assert "unparseable" in result["hint"]
    # Original file untouched.
    assert "def add(a, b):" in (repo / "pkg" / "math.py").read_text()


def test_edit_refuses_when_indent_breaks_module(tmp_path: Path) -> None:
    """Even if the new body itself parses standalone, it must not break
    the surrounding file. Here we accidentally drop the leading ``def``
    line — leaving an orphan ``return`` at module level."""
    repo = _build_repo(tmp_path)
    orphan = "    return a + b\n"
    result = edit_symbol("pkg.math:add", orphan, root=repo)
    assert result["error"] == "syntax_error"
    assert "def add" in (repo / "pkg" / "math.py").read_text()


def test_edit_then_source_reflects_new_line_range(tmp_path: Path) -> None:
    """A longer body shifts subsequent symbols' line ranges; the
    reindex inside ``edit_symbol`` must pick that up so a follow-up
    ``get_source`` on a sibling symbol still returns its real body."""
    repo = _build_repo(tmp_path)

    longer_add = (
        "def add(a, b):\n"
        '    """Sum two numbers, defensively."""\n'
        "    if a is None:\n"
        "        a = 0\n"
        "    if b is None:\n"
        "        b = 0\n"
        "    return a + b\n"
    )
    edit_symbol("pkg.math:add", longer_add, root=repo)

    mul = get_source("pkg.math:mul", root=repo)
    assert "error" not in mul
    assert mul["source"].startswith("def mul(a, b):")
    assert "return a * b" in mul["source"]
