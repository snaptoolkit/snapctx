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


def _run(argv: list[str]) -> tuple[int, dict | str, str]:
    """Invoke main() and capture stdout + stderr.

    Returns ``(exit_code, parsed_json_or_raw_stdout, stderr_text)``.
    """
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


def test_cli_walks_up_to_find_index(tmp_path: Path) -> None:
    """`snapctx context` from a nested dir should find the parent's index."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "m.py").write_text("def hello(): return 1\n")
    index_root(repo)

    deep = repo / "a" / "b"
    deep.mkdir(parents=True)
    with _at(deep):
        code, out, _ = _run(["context", "hello", "--mode", "lexical"])
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
        code, out, _ = _run(["search", "login", "-k", "5", "--mode", "lexical"])
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
    assert "building first index" in err.getvalue().lower()
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

    assert "building first index" in err1.getvalue().lower()
    # Second run finds the existing index → no bootstrap message.
    assert "building first index" not in err2.getvalue().lower()


def test_cli_query_picks_up_edits_via_incremental_refresh(tmp_path: Path) -> None:
    """A query after an edit should see the new code automatically — the CLI
    runs an incremental index_root on every query, so SHA changes get picked up.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "m.py").write_text("def alpha(): return 1\n")
    index_root(repo)

    # Edit: rename `alpha` → `beta`. Without auto-refresh, the next query
    # would still find `alpha` in the stale index.
    (repo / "m.py").write_text("def beta(): return 1\n")

    with _at(repo):
        code, out, stderr_text = _run(["search", "beta", "-k", "5", "--mode", "lexical"])

    assert code == 0
    assert isinstance(out, dict)
    qnames = {r["qname"] for r in out["results"]}
    assert "m:beta" in qnames, qnames
    # User-visible signal that a refresh happened.
    assert "refreshed index" in stderr_text.lower()


def test_cli_query_no_change_is_silent(tmp_path: Path) -> None:
    """When nothing has changed since the last index, the refresh runs but
    stays quiet — no spurious '0 updated' chatter on every query."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "m.py").write_text("def alpha(): return 1\n")
    index_root(repo)

    err = io.StringIO()
    real_err = sys.stderr
    sys.stderr = err
    try:
        with _at(repo):
            main(["search", "alpha", "--mode", "lexical"])
    finally:
        sys.stderr = real_err

    assert "refreshed index" not in err.getvalue().lower()
    assert "indexing" not in err.getvalue().lower()


def test_cli_no_source_files_does_not_create_empty_index(tmp_path: Path) -> None:
    """Running a query in a directory with NO source files at all should
    error cleanly without leaving an empty .snapctx/ behind."""
    bare = tmp_path / "empty"
    bare.mkdir()
    # No parseable source — only a binary-shaped extension.
    (bare / "noise.log").write_text("nothing to see\n")

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
        code, out, _ = _run(["roots"])
    assert code == 0
    assert isinstance(out, dict)
    assert out["mode"] == "multi"
    labels = {r["label"] for r in out["roots"]}
    assert labels == {"backend", "frontend"}


def test_cli_roots_command_no_index(tmp_path: Path) -> None:
    bare = tmp_path / "bare"
    bare.mkdir()
    with _at(bare):
        code, out, _ = _run(["roots"])
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
        code, out, _ = _run([cmd, "auth:verify"])
    assert code == 0
    assert isinstance(out, dict)
    assert out.get("root") == "backend"


def test_cli_cold_start_multi_root_at_monorepo_parent(tmp_path: Path) -> None:
    """No index anywhere, but ≥2 marker'd subdirs → bootstrap each as a root.

    Regression for: running ``snapctx context`` at a monorepo parent
    with no prior indexes silently picked the first child it found
    (or indexed the parent as one big root) instead of indexing every
    sub-project that had a project marker.
    """
    parent = tmp_path / "monorepo"
    parent.mkdir()
    backend = parent / "backend"
    backend.mkdir()
    (backend / "pyproject.toml").write_text("[project]\nname='b'\n")
    (backend / "auth.py").write_text("def verify_login(u): return True\n")
    frontend = parent / "frontend"
    frontend.mkdir()
    (frontend / "package.json").write_text("{}")
    (frontend / "app.py").write_text("def submit_login(d): return True\n")

    with _at(parent):
        code, out, err = _run(["search", "login", "-k", "5", "--mode", "lexical"])

    assert code == 0
    assert isinstance(out, dict)
    assert set(out["roots"]) == {"backend", "frontend"}
    assert (backend / ".snapctx" / "index.db").exists()
    assert (frontend / ".snapctx" / "index.db").exists()
    # Parent should not become its own indexed root.
    assert not (parent / ".snapctx").exists()
    assert "monorepo parent" in err.lower()


def test_cli_walk_down_extends_with_unindexed_marker_siblings(tmp_path: Path) -> None:
    """One sibling indexed, another marker'd but not — extend on next query.

    This is the exact failure mode that bit a real session: ``frontend/``
    had a ``.snapctx/`` from earlier work, ``backend/`` had a project
    marker but no index. Running from the monorepo parent only saw
    ``frontend/`` and the agent fell back to ``grep``.
    """
    parent = tmp_path / "monorepo"
    parent.mkdir()
    frontend = parent / "frontend"
    frontend.mkdir()
    (frontend / "package.json").write_text("{}")
    (frontend / "app.py").write_text("def submit_login(d): return True\n")
    index_root(frontend)
    backend = parent / "backend"
    backend.mkdir()
    (backend / "pyproject.toml").write_text("[project]\nname='b'\n")
    (backend / "auth.py").write_text("def verify_login(u): return True\n")
    assert not (backend / ".snapctx").exists()

    with _at(parent):
        code, out, err = _run(["search", "login", "-k", "5", "--mode", "lexical"])

    assert code == 0
    assert isinstance(out, dict)
    assert set(out["roots"]) == {"backend", "frontend"}
    assert (backend / ".snapctx" / "index.db").exists()
    assert "auto-indexing sibling sub-project" in err.lower()


def test_cli_walk_down_skips_siblings_without_project_marker(tmp_path: Path) -> None:
    """Sibling without a project marker is NOT auto-indexed — too risky.

    Otherwise a ``backups/`` or ``examples/`` dir with stray source files
    would silently get indexed every time the user queried the parent.
    """
    parent = tmp_path / "monorepo"
    parent.mkdir()
    frontend = parent / "frontend"
    frontend.mkdir()
    (frontend / "package.json").write_text("{}")
    (frontend / "app.py").write_text("def submit_login(d): return True\n")
    index_root(frontend)
    backups = parent / "backups"
    backups.mkdir()
    (backups / "snapshot.py").write_text("X = 1\n")  # source but no marker

    with _at(parent):
        code, out, _ = _run(["search", "login", "-k", "5", "--mode", "lexical"])

    assert code == 0
    assert isinstance(out, dict)
    # Either single-root response (no "roots" key) or multi-root with only
    # frontend — both prove backups was not auto-indexed. The disk check
    # below is the load-bearing assertion.
    assert set(out.get("roots", ["frontend"])) == {"frontend"}
    assert not (backups / ".snapctx").exists()


def test_cli_walk_up_does_not_trigger_sibling_extension(tmp_path: Path) -> None:
    """A query inside an already-indexed root must not auto-index siblings.

    The user has chosen a single canonical root for the whole tree;
    extending with siblings would create overlapping indexes and confuse
    qname routing.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "m.py").write_text("def hello(): return 1\n")
    index_root(repo)
    sibling = repo / "tooling"
    sibling.mkdir()
    (sibling / "pyproject.toml").write_text("[project]\nname='t'\n")
    (sibling / "build.py").write_text("def go(): pass\n")

    with _at(repo):
        code, _, _ = _run(["search", "hello", "--mode", "lexical"])

    assert code == 0
    assert not (sibling / ".snapctx").exists()


def test_cli_anchor_with_own_marker_uses_single_root_bootstrap(tmp_path: Path) -> None:
    """A regular project (anchor has its own marker) bootstraps a single root,
    even if children also have markers. Otherwise a project with a top-level
    ``pyproject.toml`` plus a ``packages/foo/package.json`` would surprise the
    user by becoming multi-root.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[project]\nname='r'\n")
    (repo / "m.py").write_text("def hello(): return 1\n")
    nested = repo / "packages" / "foo"
    nested.mkdir(parents=True)
    (nested / "package.json").write_text("{}")

    with _at(repo):
        code, out, err = _run(["search", "hello", "--mode", "lexical"])

    assert code == 0
    assert isinstance(out, dict)
    # Single-root: anchor itself was bootstrapped, no multi-root response.
    assert "roots" not in out or out.get("roots") in (None, [])
    assert (repo / ".snapctx" / "index.db").exists()
    assert "monorepo parent" not in err.lower()
