"""Multi-root fan-out for search/context/expand/source/outline.

Setup: a parent directory with two indexed sub-projects (``backend`` Python
and ``frontend`` Python — using Python on both sides keeps the test fast
and avoids depending on tree-sitter; the multi-root logic itself is
language-agnostic).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from snapctx.api import (
    context_multi,
    expand_multi,
    get_source_multi,
    index_root,
    outline_multi,
    search_code_multi,
)


@pytest.fixture
def two_roots(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A parent containing ``backend/`` and ``frontend/``, each indexed."""
    parent = tmp_path / "monorepo"
    backend = parent / "backend"
    frontend = parent / "frontend"
    backend.mkdir(parents=True)
    frontend.mkdir(parents=True)

    (backend / "auth.py").write_text(
        '"""Backend session authentication."""\n'
        "def verify_credentials(user, password):\n"
        '    """Check user credentials against the DB."""\n'
        "    return True\n"
        "class SessionManager:\n"
        "    def login(self, user, password):\n"
        "        return verify_credentials(user, password)\n"
    )

    (frontend / "ui.py").write_text(
        '"""Frontend login flow handler."""\n'
        "def handle_login_submit(form_data):\n"
        '    """Submit the login form and route to the dashboard."""\n'
        "    return True\n"
        "class LoginForm:\n"
        "    def render(self):\n"
        "        return '<form>'\n"
    )

    index_root(backend)
    index_root(frontend)
    return parent, backend, frontend


def test_search_multi_tags_results_with_root(two_roots: tuple[Path, Path, Path]) -> None:
    parent, backend, frontend = two_roots
    out = search_code_multi(
        "login", [backend, frontend], k=5, anchor=parent, mode="lexical"
    )
    assert out["roots"] == ["backend", "frontend"]
    # We should see results from both projects.
    roots_seen = {r["root"] for r in out["results"]}
    assert "backend" in roots_seen or "frontend" in roots_seen
    # Every result is tagged.
    for r in out["results"]:
        assert "root" in r
        assert r["root"] in {"backend", "frontend"}


def test_context_multi_merges_seeds_and_outlines(two_roots: tuple[Path, Path, Path]) -> None:
    parent, backend, frontend = two_roots
    out = context_multi(
        "login session", [backend, frontend], k_seeds=5, anchor=parent, mode="lexical"
    )
    assert out["roots"] == ["backend", "frontend"]
    # Each seed has a root tag.
    for s in out["seeds"]:
        assert "root" in s
    # File outlines are tagged too.
    for fo in out["file_outlines"]:
        assert "root" in fo
    # Ranks are global (1..N).
    if out["seeds"]:
        ranks = [s["rank"] for s in out["seeds"]]
        assert ranks == list(range(1, len(out["seeds"]) + 1))


def test_context_multi_seeds_sorted_by_score(two_roots: tuple[Path, Path, Path]) -> None:
    parent, backend, frontend = two_roots
    out = context_multi(
        "login", [backend, frontend], k_seeds=10, anchor=parent, mode="lexical"
    )
    scores = [float(s.get("score", 0)) for s in out["seeds"]]
    assert scores == sorted(scores, reverse=True)


def test_expand_multi_routes_to_correct_root(two_roots: tuple[Path, Path, Path]) -> None:
    parent, backend, frontend = two_roots
    # verify_credentials lives in backend.
    out = expand_multi(
        "auth:verify_credentials", [backend, frontend], anchor=parent,
        direction="callers",
    )
    assert out.get("root") == "backend"
    assert "error" not in out


def test_expand_multi_not_found(two_roots: tuple[Path, Path, Path]) -> None:
    parent, backend, frontend = two_roots
    out = expand_multi("nope:nothing", [backend, frontend], anchor=parent)
    assert out["error"] == "not_found"
    assert out["roots_tried"] == ["backend", "frontend"]


def test_get_source_multi_routes_to_correct_root(two_roots: tuple[Path, Path, Path]) -> None:
    parent, backend, frontend = two_roots
    out = get_source_multi("ui:handle_login_submit", [backend, frontend], anchor=parent)
    assert out.get("root") == "frontend"
    assert "def handle_login_submit" in out["source"]


def test_outline_multi_routes_by_path_prefix(two_roots: tuple[Path, Path, Path]) -> None:
    parent, backend, frontend = two_roots
    out = outline_multi(
        backend / "auth.py", [backend, frontend], anchor=parent
    )
    assert out.get("root") == "backend"
    qnames = {s["qname"] for s in out["symbols"]}
    assert "auth:verify_credentials" in qnames


def test_search_multi_error_labels_use_anchor(tmp_path: Path) -> None:
    """When a sub-project's index can't be opened, the per-root error
    should be labeled with the anchor-relative path, not just the basename."""
    parent = tmp_path / "monorepo"
    healthy = parent / "healthy"
    broken = parent / "broken"
    healthy.mkdir(parents=True)
    broken.mkdir(parents=True)
    (healthy / "m.py").write_text("def alpha(): return 1\n")
    index_root(healthy)
    # ``broken`` has no index — search_code raises FileNotFoundError, which
    # _fan_out catches and surfaces as a per-root error.

    out = search_code_multi(
        "alpha", [healthy, broken], k=3, anchor=parent, mode="lexical",
    )
    assert "root_errors" in out
    error_labels = [e["root"] for e in out["root_errors"]]
    # Should use the anchor-relative label "broken", not a full path.
    assert "broken" in error_labels
