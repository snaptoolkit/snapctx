"""``edit_symbol_batch``: many edits, one tool call, per-file atomic."""

from __future__ import annotations

from pathlib import Path

from snapctx.api import edit_symbol_batch, get_source, index_root


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


def test_batch_applies_multiple_edits_one_reindex(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    edits = [
        {"qname": "pkg.math:add",
         "new_body": "def add(a, b):\n    return (a + b) * 1\n"},
        {"qname": "pkg.math:mul",
         "new_body": "def mul(a, b):\n    return (a * b) * 1\n"},
        {"qname": "pkg.more:sub",
         "new_body": "def sub(a, b):\n    return (a - b) * 1\n"},
    ]
    result = edit_symbol_batch(edits, root=repo)

    assert len(result["applied"]) == 3
    assert result["errors"] == []
    assert result["files_touched"] == 2
    # One reindex for the whole batch.
    assert result["reindex"]["files_updated"] == 2

    # Each symbol now reflects the change.
    for q in ("pkg.math:add", "pkg.math:mul", "pkg.more:sub"):
        assert "(a" in get_source(q, root=repo)["source"]


def test_batch_per_file_atomicity_one_bad_doesnt_block_others(
    tmp_path: Path,
) -> None:
    repo = _build_repo(tmp_path)
    edits = [
        {"qname": "pkg.math:add",
         "new_body": "def add(a, b):\n    return a + b + 0\n"},
        # Bad: missing colon → renders the WHOLE file unparseable
        # → both math.py edits should roll back.
        {"qname": "pkg.math:mul",
         "new_body": "def mul(a, b)\n    return a * b\n"},
        # On a different file: should succeed.
        {"qname": "pkg.more:sub",
         "new_body": "def sub(a, b):\n    return a - b - 0\n"},
    ]
    result = edit_symbol_batch(edits, root=repo)

    # math.py rolls back, more.py lands.
    assert result["files_touched"] == 1
    assert any("pkg.more" in a["qname"] for a in result["applied"])
    assert not any("pkg.math" in a["qname"] for a in result["applied"])

    # The error should name the file and the syntax issue.
    assert any(
        e.get("error") == "syntax_error" and "math.py" in (e.get("hint", "") + e.get("file", ""))
        for e in result["errors"]
    )

    # math.py contents intact (unchanged), more.py modified.
    assert "return a + b\n" in (repo / "pkg" / "math.py").read_text()
    assert "(a - b) - 0" in (repo / "pkg" / "more.py").read_text() or \
           "a - b - 0" in (repo / "pkg" / "more.py").read_text()


def test_batch_unknown_qname_reported_others_proceed(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    edits = [
        {"qname": "pkg.math:nope",
         "new_body": "def nope(): pass\n"},
        {"qname": "pkg.more:sub",
         "new_body": "def sub(a, b):\n    return a - b - 1\n"},
    ]
    result = edit_symbol_batch(edits, root=repo)

    not_found = [e for e in result["errors"] if e.get("error") == "not_found"]
    assert len(not_found) == 1
    assert len(result["applied"]) == 1
    assert result["applied"][0]["qname"] == "pkg.more:sub"


def test_batch_duplicate_qname_in_same_file_rejects(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    edits = [
        {"qname": "pkg.math:add",
         "new_body": "def add(a, b):\n    return a + b\n"},
        {"qname": "pkg.math:add",
         "new_body": "def add(a, b):\n    return 0\n"},
    ]
    result = edit_symbol_batch(edits, root=repo)
    assert any(e.get("error") == "duplicate_qname" for e in result["errors"])
    assert result["applied"] == []


def test_batch_handles_within_file_line_shift(tmp_path: Path) -> None:
    """Two edits on the same file where one grows and one shrinks.
    Bottom-up application keeps line numbers stable."""
    repo = _build_repo(tmp_path)
    edits = [
        # math.py:add grows by 2 lines (its replacement is bigger).
        {"qname": "pkg.math:add",
         "new_body": "def add(a, b):\n"
                     "    if a is None:\n"
                     "        return b\n"
                     "    return a + b\n"},
        # math.py:mul shrinks to 1 line.
        {"qname": "pkg.math:mul",
         "new_body": "def mul(a, b): return a * b\n"},
    ]
    result = edit_symbol_batch(edits, root=repo)
    assert result["errors"] == []
    assert len(result["applied"]) == 2

    add_src = get_source("pkg.math:add", root=repo)
    mul_src = get_source("pkg.math:mul", root=repo)
    assert "if a is None" in add_src["source"]
    assert "def mul(a, b): return a * b" in mul_src["source"]


def test_batch_empty_input_returns_zero(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    result = edit_symbol_batch([], root=repo)
    assert result == {"applied": [], "errors": [], "files_touched": 0}
