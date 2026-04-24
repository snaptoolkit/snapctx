from __future__ import annotations

from pathlib import Path

from neargrep.api import expand, get_source, outline, search_code


def test_search_finds_refresh_method(indexed_root: Path) -> None:
    out = search_code("refresh session token", k=3, root=indexed_root)
    assert out["results"]
    top = out["results"][0]
    assert top["qname"] == "sample_pkg.auth:SessionManager.refresh"
    assert top["docstring"].startswith("Refresh")


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
