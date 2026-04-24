"""Tests for the constant-alias resolution in context()."""

from __future__ import annotations

from pathlib import Path

from neargrep.api import context, index_root


def test_alias_resolves_across_files(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    # A registry module that defines the terminal literal.
    (root / "defaults.py").write_text('DEFAULT_MODEL = "claude-opus-4-5"\n')
    # A consumer that aliases it.
    (root / "agent.py").write_text(
        "from defaults import DEFAULT_MODEL\n"
        "\n"
        "class Agent:\n"
        "    DEFAULT_MODEL = DEFAULT_MODEL\n"
    )
    index_root(root)

    out = context("Agent DEFAULT_MODEL", mode="lexical", k_seeds=5, root=root)
    # Find the aliased constant in the response.
    aliased = next(s for s in out["seeds"] if s["qname"] == "agent:Agent.DEFAULT_MODEL")
    assert "resolved_value" in aliased
    assert aliased["resolved_value"]["value"] == "'claude-opus-4-5'"
    assert aliased["resolved_value"]["terminal_qname"] == "defaults:DEFAULT_MODEL"


def test_literal_constant_is_not_resolved(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "m.py").write_text('NAME = "hello"\n')
    index_root(root)

    out = context("NAME", mode="lexical", root=root)
    hit = next(s for s in out["seeds"] if s["qname"] == "m:NAME")
    assert "resolved_value" not in hit


def test_qname_fast_path(tmp_path: Path) -> None:
    """When the query is an exact qname, skip search and return that symbol directly."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / "m.py").write_text(
        "def alpha(): pass\n"
        "def beta(): pass\n"
        "def gamma(): pass\n"
    )
    index_root(root)

    out = context("m:alpha", root=root)
    assert out["mode"] == "exact"
    assert len(out["seeds"]) == 1
    assert out["seeds"][0]["qname"] == "m:alpha"
