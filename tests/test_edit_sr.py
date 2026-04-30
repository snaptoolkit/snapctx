"""Tests for ``edit_symbol_search_replace`` and its batch variant."""

from __future__ import annotations

from pathlib import Path

from snapctx.api import (
    edit_symbol_search_replace,
    edit_symbol_search_replace_batch,
    get_source,
    index_root,
)


def _build_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "__init__.py").write_text("")
    (repo / "pkg" / "math.py").write_text(
        '"""Math helpers."""\n'
        "\n"
        "\n"
        "def add(a, b):\n"
        "    return a + b\n"
        "\n"
        "\n"
        "def mul(a, b):\n"
        "    return a * b\n"
    )
    (repo / "pkg" / "more.py").write_text(
        '"""More."""\n'
        "\n"
        "\n"
        "def sub(a, b):\n"
        "    return a - b\n"
    )
    index_root(repo)
    return repo


def test_search_replace_unique_match(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    result = edit_symbol_search_replace(
        "pkg.math:add", "a + b", "(a + b) + 0", root=repo,
    )
    assert "error" not in result, result
    assert result["chars_replaced"] == 5
    src = get_source("pkg.math:add", root=repo)["source"]
    assert "(a + b) + 0" in src


def test_search_replace_zero_match_reports_not_found(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    result = edit_symbol_search_replace(
        "pkg.math:add", "nonexistent string", "x", root=repo,
    )
    assert result["error"] == "not_found"
    assert result.get("match") == "search"


def test_search_replace_ambiguous_reports_count(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "__init__.py").write_text("")
    (repo / "pkg" / "stuff.py").write_text(
        '"""Stuff."""\n'
        "\n"
        "\n"
        "def f():\n"
        "    x = 1\n"
        "    x = 1\n"
        "    return x\n"
    )
    index_root(repo)
    result = edit_symbol_search_replace(
        "pkg.stuff:f", "x = 1", "y = 2", root=repo,
    )
    assert result["error"] == "ambiguous"
    assert result["match_count"] == 2


def test_search_replace_no_change_rejected(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    result = edit_symbol_search_replace(
        "pkg.math:add", "a + b", "a + b", root=repo,
    )
    assert result["error"] == "no_change"


def test_search_replace_syntax_preflight_blocks_bad_edit(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    # Replacing the `:` with nothing breaks the function header.
    result = edit_symbol_search_replace(
        "pkg.math:add", "def add(a, b):", "def add(a, b)", root=repo,
    )
    assert result["error"] == "syntax_error"
    # File should NOT be modified.
    assert "def add(a, b):" in (repo / "pkg" / "math.py").read_text()


def test_search_replace_unknown_qname(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    result = edit_symbol_search_replace(
        "pkg.math:nope", "x", "y", root=repo,
    )
    assert result["error"] == "not_found"


def test_batch_applies_multiple_edits_one_reindex(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    edits = [
        {"qname": "pkg.math:add", "search": "a + b", "replace": "(a + b)"},
        {"qname": "pkg.math:mul", "search": "a * b", "replace": "(a * b)"},
        {"qname": "pkg.more:sub", "search": "a - b", "replace": "(a - b)"},
    ]
    result = edit_symbol_search_replace_batch(edits, root=repo)
    assert len(result["applied"]) == 3
    assert result["errors"] == []
    assert result["files_touched"] == 2
    assert result["reindex"]["files_updated"] == 2
    assert "(a + b)" in get_source("pkg.math:add", root=repo)["source"]
    assert "(a * b)" in get_source("pkg.math:mul", root=repo)["source"]


def test_batch_per_file_atomicity(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    edits = [
        {"qname": "pkg.math:add", "search": "a + b", "replace": "a + b + 0"},
        # Bad: ambiguous on this symbol triggers per-file rollback for math.py.
        {"qname": "pkg.math:mul", "search": "a", "replace": "z"},
        {"qname": "pkg.more:sub", "search": "a - b", "replace": "a - b - 0"},
    ]
    result = edit_symbol_search_replace_batch(edits, root=repo)

    # math.py rolled back, more.py landed.
    assert any("pkg.more" in a["qname"] for a in result["applied"])
    assert not any("pkg.math" in a["qname"] for a in result["applied"])
    assert any(e["error"] == "ambiguous" for e in result["errors"])
    # math.py contents intact.
    assert "return a + b\n" in (repo / "pkg" / "math.py").read_text()


def test_batch_unknown_qname_doesnt_block_others(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    edits = [
        {"qname": "pkg.math:nope", "search": "x", "replace": "y"},
        {"qname": "pkg.more:sub", "search": "a - b", "replace": "(a - b)"},
    ]
    result = edit_symbol_search_replace_batch(edits, root=repo)
    assert any(e["error"] == "not_found" for e in result["errors"])
    assert any("pkg.more" in a["qname"] for a in result["applied"])


def test_batch_token_efficiency_vs_full_body(tmp_path: Path) -> None:
    """Sanity: search/replace payload << full-body payload for a small edit."""
    repo = _build_repo(tmp_path)
    sr_payload = {"qname": "pkg.math:add", "search": "a + b", "replace": "(a + b)"}
    full_body_payload = {
        "qname": "pkg.math:add",
        "new_body": "def add(a, b):\n    return (a + b)\n",
    }
    sr_chars = sum(len(str(v)) for v in sr_payload.values())
    fb_chars = sum(len(str(v)) for v in full_body_payload.values())
    # The whole point of this primitive is that small edits stay small.
    assert sr_chars < fb_chars
