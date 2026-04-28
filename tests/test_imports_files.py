"""``add_import`` / ``remove_import`` / ``delete_symbol`` / file ops."""

from __future__ import annotations

from pathlib import Path

from snapctx.api import (
    add_import,
    create_file,
    delete_file,
    delete_symbol,
    edit_symbol,
    get_source,
    index_root,
    move_file,
    outline,
    remove_import,
)


def _build_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "__init__.py").write_text("")
    (repo / "pkg" / "math.py").write_text(
        '"""Math helpers."""\n'
        "import os\n"
        "from pathlib import Path\n"
        "\n"
        "\n"
        "def add(a, b):\n"
        "    return a + b\n"
        "\n"
        "\n"
        "def mul(a, b):\n"
        "    return a * b\n"
    )
    index_root(repo)
    return repo


# ----- add_import / remove_import -----


def test_add_import_appends_after_existing_imports(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    result = add_import("pkg/math.py", "import sys", root=repo)
    assert "error" not in result, result
    assert result["already_present"] is False
    text = (repo / "pkg" / "math.py").read_text()
    # Lands after the existing imports, before the first def.
    assert "import sys" in text
    pos_sys = text.find("import sys")
    pos_def = text.find("def add")
    assert pos_sys < pos_def


def test_add_import_idempotent(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    r1 = add_import("pkg/math.py", "import os", root=repo)
    assert r1["already_present"] is True
    text = (repo / "pkg" / "math.py").read_text()
    assert text.count("import os\n") == 1


def test_remove_import_drops_the_line(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    result = remove_import("pkg/math.py", "import os", root=repo)
    assert "error" not in result, result
    assert result["already_absent"] is False
    text = (repo / "pkg" / "math.py").read_text()
    assert "import os" not in text
    # Other imports survive.
    assert "from pathlib import Path" in text


def test_remove_import_no_op_when_absent(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    result = remove_import("pkg/math.py", "import nope", root=repo)
    assert result["already_absent"] is True


def test_add_import_fresh_file_no_existing_imports(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "__init__.py").write_text("")
    (repo / "pkg" / "fresh.py").write_text("def x(): return 1\n")
    index_root(repo)

    result = add_import("pkg/fresh.py", "import os", root=repo)
    assert "error" not in result, result
    text = (repo / "pkg" / "fresh.py").read_text()
    assert text.startswith("import os\n")


def test_add_import_lands_after_leading_module_docstring(tmp_path: Path) -> None:
    """For a Python file whose first thing is a module docstring,
    a new import lands AFTER the docstring, not above it."""
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "__init__.py").write_text("")
    (repo / "pkg" / "docfirst.py").write_text(
        '"""Module docstring."""\n'
        "\n"
        "def x(): return 1\n"
    )
    index_root(repo)

    result = add_import("pkg/docfirst.py", "import os", root=repo)
    assert "error" not in result, result
    text = (repo / "pkg" / "docfirst.py").read_text()
    assert text.startswith('"""Module docstring."""')
    # Docstring still on line 1, import lands after it.
    assert text.split("\n")[0] == '"""Module docstring."""'
    pos_doc = text.find('"""Module docstring."""')
    pos_import = text.find("import os")
    assert pos_doc < pos_import


def test_add_import_lands_after_multiline_docstring(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "__init__.py").write_text("")
    (repo / "pkg" / "doc_multi.py").write_text(
        '"""First line.\n\n'
        "More body.\n"
        'End.\n"""\n'
        "\n"
        "def x(): return 1\n"
    )
    index_root(repo)

    result = add_import("pkg/doc_multi.py", "import os", root=repo)
    assert "error" not in result, result
    text = (repo / "pkg" / "doc_multi.py").read_text()
    assert text.startswith('"""First line.')
    # Import comes AFTER the closing """.
    pos_close = text.find('End.\n"""')
    pos_import = text.find("import os")
    assert 0 < pos_close < pos_import


def test_add_import_refuses_when_file_changed(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    target = repo / "pkg" / "math.py"
    target.write_text("# external edit\n" + target.read_text())
    result = add_import("pkg/math.py", "import sys", root=repo)
    assert result["error"] == "stale_coordinates"


# ----- delete_symbol -----


def test_delete_symbol_removes_function_and_keeps_siblings(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    result = delete_symbol("pkg.math:add", root=repo)
    assert "error" not in result, result
    assert result["lines_deleted"] >= 2  # def + body + leading blank

    text = (repo / "pkg" / "math.py").read_text()
    assert "def add" not in text
    assert "def mul" in text
    # File still parses.
    import ast as _ast
    _ast.parse(text)


def test_delete_symbol_unknown_qname(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    result = delete_symbol("pkg.math:nope", root=repo)
    assert result["error"] == "not_found"


def test_delete_symbol_then_edit_resolves_remaining_sibling(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    delete_symbol("pkg.math:add", root=repo)
    # The reindex inside delete must update line ranges so editing the
    # remaining sibling still works.
    result = edit_symbol(
        "pkg.math:mul",
        "def mul(a, b):\n    return a * b * 1\n",
        root=repo,
    )
    assert "error" not in result
    assert "a * b * 1" in get_source("pkg.math:mul", root=repo)["source"]


# ----- create_file / delete_file / move_file -----


def test_create_file_indexes_and_can_be_queried(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    result = create_file(
        "pkg/sub.py",
        "def sub(a, b):\n    return a - b\n",
        root=repo,
    )
    assert "error" not in result, result

    src = get_source("pkg.sub:sub", root=repo)
    assert "error" not in src
    assert "return a - b" in src["source"]


def test_create_file_refuses_existing_path(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    result = create_file("pkg/math.py", "x = 1\n", root=repo)
    assert result["error"] == "already_exists"


def test_create_file_syntax_guard(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    bad = "def broken(\n  return 1\n"
    result = create_file("pkg/bad.py", bad, root=repo)
    assert result["error"] == "syntax_error"
    # File was not written.
    assert not (repo / "pkg" / "bad.py").exists()


def test_delete_file_removes_and_drops_index(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    create_file("pkg/sub.py", "def sub(a, b):\n    return a - b\n", root=repo)
    result = delete_file("pkg/sub.py", root=repo)
    assert "error" not in result
    assert result["symbols_dropped"] >= 1

    # Subsequent source lookup misses cleanly.
    src = get_source("pkg.sub:sub", root=repo)
    assert src["error"] == "not_found"


def test_delete_file_outside_root_refuses(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    # An absolute path under tmp_path but NOT under repo.
    outside = tmp_path / "stray.py"
    outside.write_text("# stray\n")
    result = delete_file(str(outside), root=repo)
    assert result["error"] == "outside_root"
    # File still exists.
    assert outside.exists()


def test_move_file_renames_and_reindexes(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    # Add a sibling that imports math, so move_file can report it.
    (repo / "pkg" / "uses.py").write_text(
        "from pkg.math import add\n\n\ndef caller():\n    return add(1, 2)\n"
    )
    index_root(repo)

    result = move_file("pkg/math.py", "pkg/arithmetic.py", root=repo)
    assert "error" not in result
    assert "arithmetic.py" in result["new_file"]

    # Old file gone; new file present.
    assert not (repo / "pkg" / "math.py").exists()
    assert (repo / "pkg" / "arithmetic.py").exists()

    # New qname resolves via the new module path.
    src = get_source("pkg.arithmetic:add", root=repo)
    assert "error" not in src

    # importing_files surfaces the file that should get its import rewritten.
    assert any("uses.py" in f for f in result["importing_files"])
