"""Tests for the one-shot context() operation."""

from __future__ import annotations

from pathlib import Path

from neargrep.api import context


def test_context_returns_self_contained_pack(indexed_root: Path) -> None:
    out = context("refresh session", root=indexed_root)
    assert out["seeds"]
    top = out["seeds"][0]
    assert top["qname"] == "sample_pkg.auth:SessionManager.refresh"
    # Top seed should come with full source.
    assert "def refresh" in top["source"]
    # And with neighbors (callees/callers).
    assert top.get("callees") or top.get("callers")
    # And file outlines so siblings are visible.
    assert out["file_outlines"]
    top = out["file_outlines"][0]
    assert any(
        s["qname"] == "sample_pkg.auth:SessionManager.login"
        for s in top["symbols"]
    )


def test_context_token_estimate_reasonable(indexed_root: Path) -> None:
    out = context("refresh session", root=indexed_root)
    # Typical pack for a small fixture should be well under 4k tokens.
    assert 200 < out["token_estimate"] < 4000


def test_context_empty_query_hint(indexed_root: Path) -> None:
    out = context("xyzzy_nonsense_blarfle", mode="lexical", root=indexed_root)
    assert out["seeds"] == []
    assert "hint" in out
