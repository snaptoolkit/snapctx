"""End-to-end CLI tests for write-side subcommands.

Covers ``snapctx delete``, ``snapctx import-add``, and ``snapctx
import-remove`` — the three CLI surfaces added so a CLI-only refactor
workflow has feature parity with the session-tool workflow (issues
#13, #19). Each test invokes ``cli.main`` directly with captured
streams instead of subprocessing — same convention as
``test_cli_discovery.py``.
"""

from __future__ import annotations

import io
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path

from snapctx.api import index_root
from snapctx.cli import main


@contextmanager
def _at(path: Path):
    prev = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(prev)


def _run(argv: list[str]) -> tuple[int, dict | str, str]:
    buf = io.StringIO()
    err = io.StringIO()
    real_out, real_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = buf, err
    try:
        code = main(argv)
    finally:
        sys.stdout, sys.stderr = real_out, real_err
    text = buf.getvalue()
    try:
        return code, json.loads(text), err.getvalue()
    except json.JSONDecodeError:
        return code, text, err.getvalue()


def _build_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "m.py").write_text(
        '"""Module docstring."""\n'
        "import os\n"
        "\n"
        "\n"
        "def alpha():\n"
        "    return 1\n"
        "\n"
        "\n"
        "def beta():\n"
        "    return 2\n"
    )
    index_root(repo)
    return repo


def test_cli_delete_removes_symbol_and_reindexes(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    with _at(repo):
        code, out, _ = _run(["delete", "m:beta"])
    assert code == 0
    assert isinstance(out, dict)
    assert out["qname"] == "m:beta"
    assert out["lines_deleted"] >= 2
    assert out["reindex"]["files_updated"] == 1

    on_disk = (repo / "m.py").read_text()
    assert "def beta" not in on_disk
    assert "def alpha" in on_disk


def test_cli_delete_unknown_qname_exits_nonzero(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    with _at(repo):
        code, out, _ = _run(["delete", "m:does_not_exist"])
    assert code == 1
    assert isinstance(out, dict)
    assert out["error"] == "not_found"


def test_cli_import_add_idempotent(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    with _at(repo):
        code, out, _ = _run(["import-add", "m.py", "from typing import Any"])
    assert code == 0
    assert isinstance(out, dict)
    assert out["already_present"] is False
    assert (repo / "m.py").read_text().count("from typing import Any") == 1

    # Second add → idempotent no-op.
    with _at(repo):
        code, out, _ = _run(["import-add", "m.py", "from typing import Any"])
    assert code == 0
    assert out["already_present"] is True
    assert (repo / "m.py").read_text().count("from typing import Any") == 1


def test_cli_import_remove_drops_line(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    with _at(repo):
        code, out, _ = _run(["import-remove", "m.py", "import os"])
    assert code == 0
    assert isinstance(out, dict)
    assert out["lines_removed"] == 1
    assert "import os" not in (repo / "m.py").read_text()


def test_cli_import_remove_no_op_when_absent(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    with _at(repo):
        code, out, _ = _run(["import-remove", "m.py", "import nonexistent"])
    assert code == 0
    assert out["already_absent"] is True


def test_cli_import_add_python_lands_after_module_docstring(tmp_path: Path) -> None:
    """Regression for issue #13: docstring-aware insertion via the CLI."""
    repo = tmp_path / "repo"
    repo.mkdir()
    # No existing imports; only a module docstring.
    (repo / "fresh.py").write_text(
        '"""Module-level documentation."""\n'
        "\n"
        "def f(): return 1\n"
    )
    index_root(repo)
    with _at(repo):
        code, _, _ = _run(["import-add", "fresh.py", "import json"])
    assert code == 0
    on_disk = (repo / "fresh.py").read_text().splitlines()
    # Docstring stays first; import lands on a subsequent line.
    assert on_disk[0].startswith('"""')
    assert "import json" in on_disk
    assert on_disk.index("import json") > 0


def test_cli_delete_then_import_remove_combines(tmp_path: Path) -> None:
    """Sequential CLI write ops on the same file leave a consistent state."""
    repo = _build_repo(tmp_path)
    with _at(repo):
        c1, _, _ = _run(["delete", "m:beta"])
        c2, _, _ = _run(["import-remove", "m.py", "import os"])
    assert c1 == 0 and c2 == 0
    text = (repo / "m.py").read_text()
    assert "def beta" not in text
    assert "import os" not in text
    assert "def alpha" in text
