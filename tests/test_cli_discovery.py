"""End-to-end CLI tests for auto-discovery and multi-root fan-out.

These exercise ``snapctx.cli.main`` directly (capturing stdout) instead
of subprocessing — faster and works even if the entry point script
hasn't been re-installed.
"""

from __future__ import annotations

import io
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

from snapctx.api import index_root
from snapctx.cli import main


@contextmanager
def _at(path: Path):
    """Temporarily chdir into ``path``."""
    prev = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(prev)


def _run(argv: list[str]) -> tuple[int, dict | str]:
    """Invoke main() and capture stdout. Returns (exit_code, parsed_json_or_raw)."""
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
        return code, json.loads(text)
    except json.JSONDecodeError:
        return code, text


def test_cli_walks_up_to_find_index(tmp_path: Path) -> None:
    """`snapctx context` from a nested dir should find the parent's index."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "m.py").write_text("def hello(): return 1\n")
    index_root(repo)

    deep = repo / "a" / "b"
    deep.mkdir(parents=True)
    with _at(deep):
        code, out = _run(["context", "hello", "--mode", "lexical"])
    assert code == 0
    assert isinstance(out, dict)
    assert out["seeds"], out


def test_cli_fans_out_when_multiple_children_indexed(tmp_path: Path) -> None:
    """From a parent with two indexed children, results merge across both."""
    parent = tmp_path / "monorepo"
    backend = parent / "backend"
    frontend = parent / "frontend"
    backend.mkdir(parents=True)
    frontend.mkdir(parents=True)

    (backend / "auth.py").write_text("def verify_login(u, p): return True\n")
    (frontend / "form.py").write_text("def handle_login(d): return True\n")
    index_root(backend)
    index_root(frontend)

    with _at(parent):
        code, out = _run(["search", "login", "-k", "5", "--mode", "lexical"])
    assert code == 0
    assert isinstance(out, dict)
    assert set(out["roots"]) == {"backend", "frontend"}
    roots_in_results = {r["root"] for r in out["results"]}
    # Both projects' results should appear.
    assert "backend" in roots_in_results
    assert "frontend" in roots_in_results


def test_cli_auto_indexes_when_no_index_present(tmp_path: Path) -> None:
    """A directory with source files but no index → auto-index, then return real results."""
    bare = tmp_path / "bare"
    bare.mkdir()
    (bare / "auth.py").write_text(
        "def verify_login(user, password):\n"
        '    """Check the user credentials."""\n'
        "    return True\n"
    )

    buf = io.StringIO()
    err = io.StringIO()
    real_out, real_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = buf, err
    try:
        with _at(bare):
            code = main(["context", "verify_login", "--mode", "lexical"])
    finally:
        sys.stdout, sys.stderr = real_out, real_err

    assert code == 0
    out = json.loads(buf.getvalue())
    assert out["seeds"], out
    assert out["seeds"][0]["qname"] == "auth:verify_login"
    # The auto-index notice should have been printed to stderr.
    assert "indexing now" in err.getvalue().lower() or "building one" in err.getvalue().lower()
    # And the index should now exist on disk.
    assert (bare / ".snapctx" / "index.db").exists()


def test_cli_auto_index_subsequent_query_skips_indexing(tmp_path: Path) -> None:
    """After the first auto-index, the second query should NOT re-print the
    'no index — building' notice."""
    bare = tmp_path / "bare"
    bare.mkdir()
    (bare / "auth.py").write_text("def verify_login(): return True\n")

    err1 = io.StringIO()
    real_err = sys.stderr
    sys.stderr = err1
    try:
        with _at(bare):
            main(["context", "verify_login", "--mode", "lexical"])
    finally:
        sys.stderr = real_err

    err2 = io.StringIO()
    sys.stderr = err2
    try:
        with _at(bare):
            main(["context", "verify_login", "--mode", "lexical"])
    finally:
        sys.stderr = real_err

    assert "building one" in err1.getvalue().lower() or "indexing now" in err1.getvalue().lower()
    # No "indexing" message the second time.
    assert "indexing" not in err2.getvalue().lower()
    assert "building one" not in err2.getvalue().lower()


def test_cli_no_source_files_does_not_create_empty_index(tmp_path: Path) -> None:
    """Running a query in a directory with NO source files at all should
    error cleanly without leaving an empty .snapctx/ behind."""
    bare = tmp_path / "empty"
    bare.mkdir()
    # No .py / .ts files, just a plain text file.
    (bare / "README.txt").write_text("nothing to see\n")

    err = io.StringIO()
    real_err = sys.stderr
    sys.stderr = err
    try:
        with _at(bare):
            code = main(["context", "anything"])
    finally:
        sys.stderr = real_err

    assert code == 2
    assert "no source files" in err.getvalue().lower()
    assert not (bare / ".snapctx").exists()


def test_cli_roots_command_reports_discovery(tmp_path: Path) -> None:
    parent = tmp_path / "monorepo"
    backend = parent / "backend"
    frontend = parent / "frontend"
    backend.mkdir(parents=True)
    frontend.mkdir(parents=True)
    (backend / "m.py").write_text("def a(): pass\n")
    (frontend / "m.py").write_text("def b(): pass\n")
    index_root(backend)
    index_root(frontend)

    with _at(parent):
        code, out = _run(["roots"])
    assert code == 0
    assert isinstance(out, dict)
    assert out["mode"] == "multi"
    labels = {r["label"] for r in out["roots"]}
    assert labels == {"backend", "frontend"}


def test_cli_roots_command_no_index(tmp_path: Path) -> None:
    bare = tmp_path / "bare"
    bare.mkdir()
    with _at(bare):
        code, out = _run(["roots"])
    assert code == 1
    assert isinstance(out, dict)
    assert out["mode"] == "none"
    assert out["roots"] == []
    assert "hint" in out


@pytest.mark.parametrize("cmd", ["expand", "source"])
def test_cli_qname_routing_from_parent(tmp_path: Path, cmd: str) -> None:
    """`expand`/`source` from a parent of two indexed projects should route to the right one."""
    parent = tmp_path / "monorepo"
    backend = parent / "backend"
    frontend = parent / "frontend"
    backend.mkdir(parents=True)
    frontend.mkdir(parents=True)
    (backend / "auth.py").write_text("def verify(u): return True\n")
    (frontend / "form.py").write_text("def submit(d): return True\n")
    index_root(backend)
    index_root(frontend)

    with _at(parent):
        code, out = _run([cmd, "auth:verify"])
    assert code == 0
    assert isinstance(out, dict)
    assert out.get("root") == "backend"
