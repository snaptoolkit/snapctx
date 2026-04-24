"""Vector + hybrid search integration tests.

These load the real fastembed model (~30MB on first run) so the test suite
pays that price once per machine. Tests are not gated behind a marker because
fastembed is a hard dependency.
"""

from __future__ import annotations

from pathlib import Path

from neargrep.api import index_root, search_code


def _write_repo(root: Path) -> None:
    root.mkdir()
    (root / "auth.py").write_text(
        '"""Authentication & session module."""\n'
        "from __future__ import annotations\n"
        "\n"
        "def verify_credentials(username: str, password: str) -> bool:\n"
        '    """Check a username/password against the user store."""\n'
        "    return True\n"
        "\n"
        "def log_in(user_id: str) -> str:\n"
        '    """Create a new session cookie for an authenticated user."""\n'
        "    return 'token'\n"
    )
    (root / "ratelimit.py").write_text(
        '"""Throttle inbound API requests."""\n'
        "\n"
        "def throttle_requests(bucket: str, cost: int = 1) -> bool:\n"
        '    """Debit ``cost`` tokens from the bucket; deny if empty."""\n'
        "    return True\n"
    )


def test_indexing_generates_embeddings(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _write_repo(root)
    summary = index_root(root)
    assert summary["symbols_embedded"] >= 3


def test_vector_search_finds_paraphrased_match(tmp_path: Path) -> None:
    """Lexical search would miss 'authenticate user'; vector search shouldn't."""
    root = tmp_path / "repo"
    _write_repo(root)
    index_root(root)

    out = search_code("authenticate user", mode="vector", k=3, root=root)
    qnames = [r["qname"] for r in out["results"]]
    assert "auth:verify_credentials" in qnames


def test_vector_search_finds_rate_limit_by_synonym(tmp_path: Path) -> None:
    """`rate limit` is not in the code; `throttle` is. Vector search bridges it."""
    root = tmp_path / "repo"
    _write_repo(root)
    index_root(root)

    out = search_code("rate limit api calls", mode="vector", k=3, root=root)
    qnames = [r["qname"] for r in out["results"]]
    assert "ratelimit:throttle_requests" in qnames


def test_hybrid_search_returns_results(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _write_repo(root)
    index_root(root)

    out = search_code("log in", mode="hybrid", k=3, root=root)
    assert out["mode"] == "hybrid"
    assert out["results"]
