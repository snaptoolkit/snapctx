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


def test_add_import_auto_recovers_when_file_changed(tmp_path: Path) -> None:
    """SHA drift triggers an inline single-file re-parse so the import
    still lands cleanly. Regression for #15."""
    repo = _build_repo(tmp_path)
    target = repo / "pkg" / "math.py"
    target.write_text("# external edit\n" + target.read_text())
    result = add_import("pkg/math.py", "import sys", root=repo)
    assert "error" not in result, result
    assert "import sys" in target.read_text()
    # External change still present — we recovered, not overwrote.
    assert target.read_text().startswith("# external edit")


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


def test_parallel_writes_do_not_raise_database_locked(tmp_path: Path) -> None:
    """Regression for #10. Multiple concurrent write ops against the same
    repo were nondeterministically failing with ``OperationalError:
    database is locked`` because each writer opened its own SQLite
    connection and the second writer hit the active write txn before
    SQLite's internal busy timer kicked in. Index now sets
    ``PRAGMA busy_timeout`` so contended writers wait their turn rather
    than fail.
    """
    import concurrent.futures

    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "__init__.py").write_text("")
    # Several files so each thread targets a different one — same DB,
    # different file rows. That's the exact contention shape the bug hit.
    for i in range(8):
        (repo / "pkg" / f"mod_{i}.py").write_text(
            "import os\n\n\ndef f():\n    return 1\n"
        )
    index_root(repo)

    def add_one(i: int) -> dict:
        return add_import(f"pkg/mod_{i}.py", "import sys", root=repo)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(add_one, range(8)))

    assert all("error" not in r for r in results), results
    for i in range(8):
        assert "import sys" in (repo / "pkg" / f"mod_{i}.py").read_text()


def test_add_import_after_multiline_python_import_block(tmp_path: Path) -> None:
    """Regression for issue #24: a multi-line ``from x import (a, b)``
    statement records the START line in the imports table; ``add_import``
    used to insert at ``max(line) + 0`` and split the statement in
    half. The new import must land AFTER the closing ``)``."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "m.py").write_text(
        "from typing import (\n"
        "    Any,\n"
        "    Dict,\n"
        "    List,\n"
        ")\n"
        "\n"
        "from os import (\n"
        "    path,\n"
        "    sep,\n"
        ")\n"
        "\n"
        "\n"
        "def f(): pass\n"
    )
    index_root(repo)

    result = add_import("m.py", "import json", root=repo)
    assert "error" not in result, result
    text = (repo / "m.py").read_text()
    # The original multi-line imports must be intact — `from os import`
    # block still ends with `)` on its own line, untouched.
    assert "from os import (\n    path,\n    sep,\n)\n" in text
    # And `import json` lands after the second multi-line block.
    pos_close = text.index("\n)\n", text.index("from os import"))
    pos_new = text.index("import json")
    assert pos_close < pos_new


def test_add_import_after_multiline_typescript_import_block(tmp_path: Path) -> None:
    """Same regression for TS ``import { ... } from "..."`` multi-line
    blocks: the imports table records the start line; the insert point
    must be the actual end of the statement, not the start."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "planningActions.ts").write_text(
        "import {\n"
        "  foo,\n"
        "  bar,\n"
        "} from \"./utils\"\n"
        "\n"
        "import type {\n"
        "  DeviceData,\n"
        "} from \"./types\"\n"
        "\n"
        "export function run(): void {}\n"
    )
    index_root(repo)

    result = add_import(
        "planningActions.ts",
        'import { newThing } from "./newPlace"',
        root=repo,
    )
    assert "error" not in result, result
    text = (repo / "planningActions.ts").read_text()
    # Both original multi-line imports must be intact.
    assert 'import {\n  foo,\n  bar,\n} from "./utils"\n' in text
    assert 'import type {\n  DeviceData,\n} from "./types"\n' in text
    # The new line lands after the second block, before the function.
    pos_types_close = text.index('} from "./types"')
    pos_new = text.index('import { newThing } from "./newPlace"')
    pos_fn = text.index("export function run()")
    assert pos_types_close < pos_new < pos_fn
