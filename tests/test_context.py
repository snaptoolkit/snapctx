"""Tests for the one-shot context() operation."""

from __future__ import annotations

from pathlib import Path

from snapctx.api import context


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


def test_context_depth_2_nests_call_path(tmp_path: Path) -> None:
    """expand_depth=2 should nest depth-2 callees under each depth-1 resolved
    callee, so an agent sees the full trace (A → B → C) in one payload."""
    from snapctx.api import index_root

    (tmp_path / "m.py").write_text(
        "def leaf():\n"
        "    return 3\n"
        "def middle():\n"
        "    return leaf()\n"
        "def root_fn():\n"
        "    return middle()\n"
    )
    index_root(tmp_path)

    out = context("root_fn", root=tmp_path, expand_depth=2, mode="lexical")
    seed = next(s for s in out["seeds"] if s["qname"] == "m:root_fn")
    # Depth-1 callee is middle().
    assert seed["callees"] and seed["callees"][0]["qname"] == "m:middle"
    # Depth-2 is leaf(), nested under middle.
    middle_entry = seed["callees"][0]
    assert middle_entry.get("callees")
    assert middle_entry["callees"][0]["qname"] == "m:leaf"


def test_context_depth_1_flat_by_default_when_asked(tmp_path: Path) -> None:
    """expand_depth=1 leaves callees flat (no nested ``callees``)."""
    from snapctx.api import index_root

    (tmp_path / "m.py").write_text(
        "def leaf(): return 3\n"
        "def middle(): return leaf()\n"
        "def root_fn(): return middle()\n"
    )
    index_root(tmp_path)

    out = context("root_fn", root=tmp_path, expand_depth=1, mode="lexical")
    seed = next(s for s in out["seeds"] if s["qname"] == "m:root_fn")
    middle_entry = seed["callees"][0]
    assert middle_entry["qname"] == "m:middle"
    assert "callees" not in middle_entry


def test_context_drops_js_method_dispatch_noise(tmp_path: Path) -> None:
    """Unresolved `X.forEach`, `map.set`, `arr.push`, `promise.then`, etc. are
    stdlib method dispatch — noise in a call graph, drop from context()."""
    from snapctx.api import _is_builtin_noise

    # JS dispatch on unknown objects → drop
    assert _is_builtin_noise("?:arr.forEach")
    assert _is_builtin_noise("?:ids.map")
    assert _is_builtin_noise("?:map.set")
    assert _is_builtin_noise("?:set.has")
    assert _is_builtin_noise("?:channels.push")
    assert _is_builtin_noise("?:promise.then")
    assert _is_builtin_noise("?:JSON.parse")
    assert _is_builtin_noise("?:clearTimeout")

    # Domain method calls → keep
    assert not _is_builtin_noise("?:redis.publish")
    assert not _is_builtin_noise("?:conn.disconnect")
    assert not _is_builtin_noise("?:logger.info")
    # Resolved callees → not noise regardless
    assert not _is_builtin_noise("module:function")


def test_context_drops_builtin_noise_from_callees(tmp_path: Path) -> None:
    """Unresolved calls to Python builtins (print, len, …) are noise in a
    call graph — they shouldn't crowd out real neighbors."""
    from snapctx.api import index_root

    (tmp_path / "m.py").write_text(
        "def helper(): return 1\n"
        "def target():\n"
        "    print('hi')\n"
        "    x = len([])\n"
        "    isinstance(x, int)\n"
        "    return helper()\n"
    )
    index_root(tmp_path)

    out = context("target", root=tmp_path, mode="lexical")
    seed = next(s for s in out["seeds"] if s["qname"] == "m:target")
    callee_qnames = {c["qname"] for c in seed.get("callees", [])}
    # Only helper() should remain; print/len/isinstance are builtin noise.
    assert "m:helper" in callee_qnames
    assert not any(c.startswith("?:") and c.split(":")[1] in {"print", "len", "isinstance"}
                   for c in callee_qnames)
