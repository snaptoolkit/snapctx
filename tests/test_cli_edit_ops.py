"""End-to-end CLI tests for the new edit / file-CRUD subcommands.

Exercises ``snapctx.cli.main`` directly (faster than subprocessing) for
the commands that did not previously have CLI exposure: ``edit-sr``,
``edit-sr-batch``, ``edit-batch``, ``create-file``, ``delete-file``,
``move-file``. The api primitives behind them are already covered by
their own unit tests; these tests verify the CLI wiring — argv
parsing, stdin / file routing, exit codes — works end-to-end.

Why these matter in CLI form: they are the surface an LLM-driven
agent (Claude Code, opencode, etc.) actually touches. A primitive that
lives only in Python is invisible to a Bash-only agent harness.
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


@contextmanager
def _piped_stdin(text: str):
    real = sys.stdin
    sys.stdin = io.StringIO(text)
    try:
        yield
    finally:
        sys.stdin = real


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
    index_root(repo)
    return repo


# ---------- edit-sr ----------


def test_edit_sr_replaces_substring(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    with _at(repo):
        code, out, _ = _run([
            "edit-sr", "pkg.math:add",
            "return a + b", "return a + b + 0",
        ])
    assert code == 0, out
    assert isinstance(out, dict)
    assert "error" not in out
    body = (repo / "pkg" / "math.py").read_text()
    assert "return a + b + 0" in body


def test_edit_sr_not_found_surfaces_error(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    with _at(repo):
        code, out, _ = _run([
            "edit-sr", "pkg.math:add",
            "this string is not in the body", "...",
        ])
    assert code == 1
    assert isinstance(out, dict)
    assert out.get("error")


# ---------- edit-sr-batch ----------


def test_edit_sr_batch_via_stdin(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    edits = [
        {"qname": "pkg.math:add", "search": "a + b", "replace": "(a + b)"},
        {"qname": "pkg.math:mul", "search": "a * b", "replace": "(a * b)"},
    ]
    with _at(repo), _piped_stdin(json.dumps(edits)):
        code, out, _ = _run(["edit-sr-batch", "--stdin"])
    assert code == 0, out
    assert isinstance(out, dict)
    assert len(out["applied"]) == 2
    body = (repo / "pkg" / "math.py").read_text()
    assert "return (a + b)" in body
    assert "return (a * b)" in body


def test_edit_sr_batch_via_file(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    edits_path = tmp_path / "edits.json"
    edits_path.write_text(json.dumps([
        {"qname": "pkg.math:add", "search": "a + b", "replace": "a+b"},
    ]))
    with _at(repo):
        code, out, _ = _run(["edit-sr-batch", str(edits_path)])
    assert code == 0, out
    assert len(out["applied"]) == 1


# ---------- edit-batch ----------


def test_edit_batch_via_stdin(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    edits = [{
        "qname": "pkg.math:add",
        "new_body": "def add(a, b):\n    return a + b + 1\n",
    }]
    with _at(repo), _piped_stdin(json.dumps(edits)):
        code, out, _ = _run(["edit-batch", "--stdin"])
    assert code == 0, out
    assert len(out["applied"]) == 1
    body = (repo / "pkg" / "math.py").read_text()
    assert "return a + b + 1" in body


# ---------- --body flag (edit / insert) ----------


def test_edit_via_body_flag(tmp_path: Path) -> None:
    """``--body`` accepts an inline string — saves agents the round
    trip of switching to --stdin after a positional path fails."""
    repo = _build_repo(tmp_path)
    new_body = "def add(a, b):\n    return a + b + 100\n"
    with _at(repo):
        code, out, _ = _run([
            "edit", "pkg.math:add", "--body", new_body,
        ])
    assert code == 0, out
    assert "return a + b + 100" in (repo / "pkg" / "math.py").read_text()


def test_insert_via_body_flag(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    new_fn = "def sq(a):\n    return a * a\n"
    with _at(repo):
        code, out, _ = _run([
            "insert", "pkg.math:mul", "--body", new_fn, "--position", "after",
        ])
    assert code == 0, out
    text = (repo / "pkg" / "math.py").read_text()
    # Whitespace normalization should have produced exactly 2 blank
    # lines between mul's body and the new top-level sq def.
    assert "    return a * b\n\n\ndef sq" in text


def test_edit_rejects_multiple_body_sources(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    with _at(repo):
        code, _, err = _run([
            "edit", "pkg.math:add",
            "--body", "x", "--stdin",
        ])
    assert code == 2
    assert "at most one" in err.lower()


# ---------- create-file ----------


def test_create_file_via_stdin(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    content = "def hello():\n    return 'hi'\n"
    with _at(repo), _piped_stdin(content):
        code, out, _ = _run(["create-file", "pkg/greeting.py", "--stdin"])
    assert code == 0, out
    assert (repo / "pkg" / "greeting.py").read_text() == content


def test_create_file_via_content_flag(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    payload = "def hello():\n    return 'hi'\n"
    with _at(repo):
        code, out, _ = _run([
            "create-file", "pkg/greeting.py", "--content", payload,
        ])
    assert code == 0, out
    assert (repo / "pkg" / "greeting.py").read_text() == payload


def test_create_file_refuses_existing(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    with _at(repo), _piped_stdin("dummy"):
        code, out, _ = _run(["create-file", "pkg/math.py", "--stdin"])
    assert code == 1
    assert isinstance(out, dict) and out.get("error")


# ---------- delete-file ----------


def test_delete_file_drops_from_disk_and_index(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    with _at(repo):
        code, out, _ = _run(["delete-file", "pkg/math.py"])
    assert code == 0, out
    assert not (repo / "pkg" / "math.py").exists()


def test_delete_file_outside_root_refused(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("x")
    with _at(repo):
        code, out, _ = _run(["delete-file", str(outside)])
    assert code == 1
    assert isinstance(out, dict) and out.get("error") == "outside_root"
    assert outside.exists()  # unchanged


# ---------- move-file ----------


def test_move_file_renames_and_reindexes(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    with _at(repo):
        code, out, _ = _run([
            "move-file", "pkg/math.py", "pkg/arithmetic.py",
        ])
    assert code == 0, out
    assert not (repo / "pkg" / "math.py").exists()
    assert (repo / "pkg" / "arithmetic.py").exists()
