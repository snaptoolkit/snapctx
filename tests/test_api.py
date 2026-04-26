from __future__ import annotations

from pathlib import Path

from snapctx.api import expand, get_source, outline, search_code


def test_search_finds_refresh_method(indexed_root: Path) -> None:
    out = search_code("refresh session token", k=3, root=indexed_root)
    assert out["results"]
    top = out["results"][0]
    assert top["qname"] == "sample_pkg.auth:SessionManager.refresh"
    assert top["docstring"].startswith("Refresh")


def test_search_with_bodies_inlines_source(indexed_root: Path) -> None:
    """``--with-bodies`` is the audit-class one-shot path: ranked symbols
    arrive with full source bodies inline so the agent doesn't have to
    chase each hit with a separate ``get_source`` round-trip."""
    out = search_code(
        "refresh session token", k=3, root=indexed_root, with_bodies=True,
    )
    assert out["results"]
    for hit in out["results"]:
        assert "source" in hit, f"missing source on hit {hit['qname']}"
        assert hit["source"], f"empty source on hit {hit['qname']}"


def test_search_without_bodies_omits_source(indexed_root: Path) -> None:
    """Default behavior unchanged — no ``source`` field unless asked."""
    out = search_code("refresh session token", k=3, root=indexed_root)
    for hit in out["results"]:
        assert "source" not in hit


def test_search_with_bodies_caps_long_bodies(indexed_root: Path) -> None:
    """Bodies are truncated past ``body_char_cap`` so a single audit
    call doesn't return arbitrarily large payloads."""
    out = search_code(
        "refresh session token", k=3, root=indexed_root,
        with_bodies=True, body_char_cap=50,
    )
    for hit in out["results"]:
        # 50-char cap + a small "truncated" footer; cap is generous to
        # keep the test stable across formatter changes.
        assert len(hit["source"]) < 200


def test_exact_name_token() -> None:
    """Single identifier-shaped tokens trigger the name-match bonus; multi-word
    queries don't."""
    from snapctx.api._ranking import _exact_name_token

    assert _exact_name_token("Button") == "button"
    assert _exact_name_token("url_for") == "url_for"
    assert _exact_name_token("StateCreator") == "statecreator"
    # Multi-word natural queries: no bonus.
    assert _exact_name_token("how does session prepare") is None
    assert _exact_name_token("rate limit") is None
    # Single word that isn't identifier-shaped: still accepted (Button isn't
    # camelCase but is a common identifier).
    assert _exact_name_token("Foo") == "foo"
    # Empty / single-char: rejected.
    assert _exact_name_token("") is None
    assert _exact_name_token("x") is None


def test_rrf_name_match_bonus_promotes_canonical_def() -> None:
    """Real bug from evaluating snapctx on flask: lexical FTS5 ranks five
    ``test_url_for_*`` methods above ``flask.helpers:url_for`` because their
    docstrings/signatures all contain the token. The name-match bonus pulls
    the canonical definition back to #1."""
    from snapctx.api._ranking import rrf_merge

    def row(qname: str, file: str = "/repo/src/x.py") -> dict:
        return {"qname": qname, "file": file}

    # Lexical pretends url_for is buried under five test methods.
    lex = [
        (row("tests.t:TestUrlFor.test_url_for_with_anchor", "/repo/tests/t.py"), -6.9),
        (row("tests.t:TestUrlFor.test_url_for_with_scheme", "/repo/tests/t.py"), -6.9),
        (row("tests.t:TestUrlFor.test_url_for_with_self",   "/repo/tests/t.py"), -6.9),
        (row("flask.helpers:url_for"),                                            -3.0),
    ]
    # Vector correctly puts the canonical def at #1.
    vec = [
        (row("flask.helpers:url_for"), 0.71),
        (row("tests.t:TestUrlFor.test_url_for_with_self.index", "/repo/tests/t.py"), 0.70),
    ]
    merged = rrf_merge(lex, vec, limit=3, query="url_for")
    assert merged[0][0]["qname"] == "flask.helpers:url_for"


def test_rrf_no_query_no_bonus() -> None:
    """Backwards compatibility: callers that don't pass ``query`` get the
    plain RRF (only test penalty)."""
    from snapctx.api._ranking import rrf_merge

    def row(qname: str) -> dict:
        return {"qname": qname, "file": "/x.py"}

    lex = [(row("a:foo"), -3.0), (row("a:bar"), -2.0)]
    vec = [(row("a:bar"), 0.7), (row("a:foo"), 0.6)]
    merged = rrf_merge(lex, vec, limit=2)
    # Without a query, ordering is the unbiased RRF (top of both lists wins).
    assert {r["qname"] for r, _ in merged} == {"a:foo", "a:bar"}


def test_query_classifier() -> None:
    """Ranker should classify queries by shape and adjust weights."""
    from snapctx.api import _classify_query

    # Identifier-shape: camelCase, snake_case, dotted, CONSTANT
    assert _classify_query("run_exscript") == "identifier"
    assert _classify_query("SessionManager") == "identifier"
    assert _classify_query("apps.auth:login") == "identifier"
    assert _classify_query("DEFAULT_MODEL") == "identifier"
    assert _classify_query("run_exscript task") == "identifier"

    # Natural language: many tokens with stopwords.
    assert _classify_query("how does the user authenticate") == "natural"
    assert _classify_query("where is the rate limiter applied") == "natural"

    # Mixed / short freeform: no obvious identifier shape, few stopwords.
    assert _classify_query("rate limit") == "mixed"
    assert _classify_query("throttle requests") == "mixed"


def test_search_filters_by_kind(indexed_root: Path) -> None:
    out = search_code("session", kind="class", root=indexed_root)
    assert out["results"]
    assert all(r["kind"] == "class" for r in out["results"])


def test_search_empty_has_hint(indexed_root: Path) -> None:
    """In lexical mode, a query with no token overlap returns zero results.

    (Vector/hybrid modes always return *something* — top-k by cosine similarity
    — so the empty-hint invariant only applies to the pure lexical path.)
    """
    out = search_code("nonsense_xyzzy_flibberflabber", mode="lexical", root=indexed_root)
    assert out["results"] == []
    assert "synonyms" in out["hint"].lower() or "no match" in out["hint"].lower()


def test_expand_callees(indexed_root: Path) -> None:
    out = expand("sample_pkg.auth:SessionManager.refresh", root=indexed_root)
    layer = out["layers"][0]
    qnames = {e.get("qname") for e in layer}
    assert "sample_pkg.utils:hash_token" in qnames


def test_expand_callers_reverse(indexed_root: Path) -> None:
    out = expand(
        "sample_pkg.utils:hash_token", direction="callers", root=indexed_root
    )
    layer = out["layers"][0]
    qnames = {e["qname"] for e in layer}
    assert qnames == {
        "sample_pkg.auth:SessionManager.login",
        "sample_pkg.auth:SessionManager.refresh",
    }


def test_expand_unknown_qname_returns_hint(indexed_root: Path) -> None:
    out = expand("does.not:exist", root=indexed_root)
    assert out["error"] == "not_found"


def test_outline_nests_methods_under_class(indexed_root: Path) -> None:
    out = outline("sample_pkg/auth.py", root=indexed_root)
    assert out["symbols"]
    classes = {s["qname"]: s for s in out["symbols"]}
    sm = classes["sample_pkg.auth:SessionManager"]
    method_qnames = {c["qname"] for c in sm["children"]}
    assert "sample_pkg.auth:SessionManager.login" in method_qnames
    assert "sample_pkg.auth:SessionManager.refresh" in method_qnames


def test_get_source_returns_body(indexed_root: Path) -> None:
    out = get_source("sample_pkg.utils:hash_token", root=indexed_root)
    assert "def hash_token" in out["source"]
    assert "hashlib.sha256" in out["source"]


def test_get_source_with_neighbors_lists_callees(indexed_root: Path) -> None:
    out = get_source(
        "sample_pkg.auth:SessionManager.refresh",
        with_neighbors=True,
        root=indexed_root,
    )
    callees = {c["qname"] for c in out["callees"]}
    assert "sample_pkg.utils:hash_token" in callees
