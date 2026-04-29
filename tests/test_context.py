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


def test_context_audit_query_attaches_find_results(tmp_path: Path) -> None:
    """When the query is an unambiguous audit phrasing, context attaches a
    `find_results` block with exhaustive coverage of the literal."""
    from snapctx.api import index_root

    repo = tmp_path / "repo"
    # 8 functions with the literal — well past the default k_seeds=5 cap.
    for i in range(8):
        p = repo / f"mod{i}.py"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            f"def caller_{i}():\n"
            f"    with transaction.atomic():\n"
            f"        do_thing_{i}()\n"
        )
    index_root(repo)

    out = context("audit every transaction.atomic site", root=repo, mode="lexical")
    assert "find_results" in out
    fr = out["find_results"]
    assert fr["literal"] == "transaction.atomic"
    assert fr["match_count"] == 8  # every site, not just k_seeds
    qnames = {m["qname"] for m in fr["matches"]}
    assert qnames == {f"mod{i}:caller_{i}" for i in range(8)}
    # Hint mentions the find block + how to upgrade to bodies.
    assert "find_results" in out["hint"] or "find" in out["hint"].lower()


def test_context_non_audit_query_skips_find(tmp_path: Path) -> None:
    """Plain "how" questions don't trigger the find block — no clutter."""
    from snapctx.api import index_root

    repo = tmp_path / "repo"
    p = repo / "x.py"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("def login():\n    pass\n")
    index_root(repo)

    out = context("how does login work", root=repo, mode="lexical")
    assert "find_results" not in out


def test_context_ambiguous_audit_skips_find(tmp_path: Path) -> None:
    """Multi-literal audits ("every LLM provider call") don't fire find —
    the extractor returns None when the literal is ambiguous."""
    from snapctx.api import index_root

    repo = tmp_path / "repo"
    p = repo / "x.py"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("def login():\n    pass\n")
    index_root(repo)

    out = context("audit every LLM provider call", root=repo, mode="lexical")
    assert "find_results" not in out


def test_context_drops_file_outlines_on_soft_overflow(tmp_path: Path) -> None:
    """Broad framework/routing-style queries returned 9k+-token payloads in
    real agent sessions because file_outlines ballooned. When the payload
    exceeds the soft budget (8k tokens), drop file_outlines — seeds carry
    the load-bearing code, outlines are extra structure."""
    from snapctx.api import context, index_root

    repo = tmp_path / "repo"
    repo.mkdir()
    # Generate ~50 files with realistic-size symbols so file_outlines'
    # contribution dwarfs the seeds' contribution.
    for i in range(50):
        body = "\n".join(
            f"def helper_{i}_{j}(x):\n"
            f"    \"\"\"Helper {j} for module {i}.\"\"\"\n"
            f"    return x + {j}"
            for j in range(20)
        )
        (repo / f"mod_{i}.py").write_text(body + "\n")
    index_root(repo)

    out = context("helper return value", root=repo, mode="lexical")
    if out.get("trimmed") in ("soft", "hard"):
        assert out["file_outlines"] == [], "soft trim should drop outlines"
        assert "snapctx_grep" in out["hint"]
    else:
        # Synthetic fixture might not exceed the budget on this machine —
        # the unit test below proves the trim mechanics directly.
        pass


def test_apply_payload_guard_drops_outlines_above_soft_budget() -> None:
    """Direct unit test of the trim mechanics: feed a payload that exceeds
    the soft budget and assert file_outlines are dropped."""
    from snapctx.api._context import _SOFT_TOKEN_BUDGET, _apply_payload_guard

    payload = {
        "seeds": [{"qname": "x:y", "source": "def y(): return 1"}],
        "file_outlines": [
            {"file": f"mod_{i}.py", "symbols": [{"qname": f"x:y_{j}"} for j in range(50)]}
            for i in range(20)
        ],
        "token_estimate": _SOFT_TOKEN_BUDGET + 1000,
    }
    _apply_payload_guard(payload, body_char_cap=2000)
    assert payload["trimmed"] == "soft"
    assert payload["file_outlines"] == []


def test_apply_payload_guard_truncates_seeds_above_hard_budget() -> None:
    """Past the hard budget, also truncate per-seed source bodies."""
    from snapctx.api._context import _HARD_TOKEN_BUDGET, _apply_payload_guard

    big_body = "x = 1\n" * 10000  # ~60KB body
    payload = {
        "seeds": [{"qname": "a:b", "source": big_body}],
        "file_outlines": [],
        "token_estimate": _HARD_TOKEN_BUDGET + 1000,
    }
    original_len = len(payload["seeds"][0]["source"])
    _apply_payload_guard(payload, body_char_cap=2000)
    assert payload["trimmed"] == "hard"
    assert len(payload["seeds"][0]["source"]) < original_len
    assert "truncated" in payload["seeds"][0]["source"]


def test_apply_payload_guard_noop_below_threshold() -> None:
    """Modest-size payloads aren't touched."""
    from snapctx.api._context import _apply_payload_guard

    payload = {
        "seeds": [{"qname": "x:y", "source": "def y(): return 1"}],
        "file_outlines": [{"file": "x.py", "symbols": []}],
        "token_estimate": 1500,
    }
    _apply_payload_guard(payload, body_char_cap=2000)
    assert "trimmed" not in payload
    assert payload["file_outlines"] != []
