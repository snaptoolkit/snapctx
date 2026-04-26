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


def test_search_with_bodies_inlines_referenced_constants(tmp_path: Path) -> None:
    """A function body that references SCREAMING_SNAKE constants gets each
    one's terminal literal inlined as ``referenced_constants``, so the
    agent doesn't have to chase the constant in a separate round-trip."""
    from snapctx.api import index_root

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "defaults.py").write_text(
        "DEFAULT_MODEL = 'claude-opus-4-5'\n"
        "DEFAULT_TEMP = 0.5\n"
    )
    (repo / "service.py").write_text(
        "from defaults import DEFAULT_MODEL, DEFAULT_TEMP\n"
        "\n"
        "def call_anthropic(prompt):\n"
        "    return client.messages.create(\n"
        "        model=DEFAULT_MODEL,\n"
        "        temperature=DEFAULT_TEMP,\n"
        "        messages=[{'role': 'user', 'content': prompt}],\n"
        "    )\n"
    )
    index_root(repo)

    out = search_code("call anthropic", k=2, root=repo, with_bodies=True)
    hit = next(h for h in out["results"] if h["qname"].endswith(":call_anthropic"))
    consts = hit.get("referenced_constants") or {}
    assert "DEFAULT_MODEL" in consts
    assert "claude-opus-4-5" in consts["DEFAULT_MODEL"]["value"]
    assert "DEFAULT_TEMP" in consts


def test_search_with_bodies_skips_constants_when_off(tmp_path: Path) -> None:
    """The enrichment is paired with ``with_bodies``; default search
    response stays unchanged."""
    from snapctx.api import index_root

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "defaults.py").write_text("DEFAULT_MODEL = 'claude-opus-4-5'\n")
    (repo / "service.py").write_text(
        "from defaults import DEFAULT_MODEL\n"
        "def call_anthropic(): return DEFAULT_MODEL\n"
    )
    index_root(repo)

    out = search_code("call anthropic", k=2, root=repo)  # no with_bodies
    for hit in out["results"]:
        assert "referenced_constants" not in hit


def test_search_also_unions_multi_term_results(tmp_path: Path) -> None:
    """``also=[...]`` runs the search across multiple terms in ONE call,
    deduping and merging — exactly the audit-class case where the agent
    today fires N separate searches for N keywords."""
    from snapctx.api import index_root

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "anth.py").write_text("def call_anthropic_api(): pass\n")
    (repo / "oai.py").write_text("def call_openai_api(): pass\n")
    (repo / "gem.py").write_text("def call_gemini_api(): pass\n")
    index_root(repo)

    out = search_code(
        "anthropic", k=10, root=repo, also=["openai", "gemini"],
    )
    qnames = {r["qname"] for r in out["results"]}
    assert "anth:call_anthropic_api" in qnames
    assert "oai:call_openai_api" in qnames
    assert "gem:call_gemini_api" in qnames
    assert out["also"] == ["openai", "gemini"]


def test_search_also_dedupes_when_terms_overlap(tmp_path: Path) -> None:
    """A symbol matched by two different terms should appear once, not twice."""
    from snapctx.api import index_root

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "x.py").write_text("def anthropic_openai_handler(): pass\n")
    index_root(repo)

    out = search_code("anthropic", k=10, root=repo, also=["openai"])
    qnames = [r["qname"] for r in out["results"]]
    assert qnames.count("x:anthropic_openai_handler") == 1


def test_outline_directory_returns_every_indexed_file(tmp_path: Path) -> None:
    """``outline <dir>`` enumerates every indexed file under the directory.

    This is the audit-by-enumeration path: when an agent needs to be
    exhaustive (every middleware, every model, every command) and ranking
    might miss long-tail symbols, directory-mode outline returns the
    structural truth — every indexed file's tree, in one call.
    """
    from snapctx.api import index_root

    repo = tmp_path / "repo"
    (repo / "middleware").mkdir(parents=True)
    (repo / "middleware" / "persist.py").write_text("def persist(): pass\n")
    (repo / "middleware" / "devtools.py").write_text("def devtools(): pass\n")
    (repo / "middleware" / "immer.py").write_text("def immer(): pass\n")
    (repo / "app.py").write_text("def main(): pass\n")
    index_root(repo)

    out = outline(repo / "middleware", root=repo)
    assert "files" in out
    assert out["file_count"] == 3
    file_basenames = {Path(f["file"]).name for f in out["files"]}
    assert file_basenames == {"persist.py", "devtools.py", "immer.py"}


def test_outline_directory_with_bodies_inlines_each_top_level(tmp_path: Path) -> None:
    """``outline <dir> --with-bodies`` inlines every top-level symbol's
    source body, so an audit of "every X in this folder" lands the
    bodies it needs without N follow-up ``source`` calls."""
    from snapctx.api import index_root

    repo = tmp_path / "repo"
    (repo / "middleware").mkdir(parents=True)
    (repo / "middleware" / "persist.py").write_text(
        "def persist():\n    return 'persisting'\n"
    )
    (repo / "middleware" / "devtools.py").write_text(
        "def devtools():\n    return 'devtooling'\n"
    )
    index_root(repo)

    out = outline(repo / "middleware", root=repo, with_bodies=True)
    by_file = {Path(f["file"]).name: f for f in out["files"]}
    persist_root = next(s for s in by_file["persist.py"]["symbols"]
                        if s["qname"].endswith(":persist"))
    assert "source" in persist_root
    assert "persisting" in persist_root["source"]


def test_outline_directory_truncates_at_max_files(tmp_path: Path) -> None:
    from snapctx.api import index_root

    repo = tmp_path / "repo"
    repo.mkdir()
    for i in range(5):
        (repo / f"m{i}.py").write_text(f"def fn{i}(): pass\n")
    index_root(repo)

    out = outline(repo, root=repo, max_files=2)
    assert out["file_count"] == 2
    assert out.get("truncated") is True
    assert out.get("total_files") == 5


def test_outline_file_mode_unchanged(indexed_root: Path) -> None:
    """File-mode outline keeps its existing response shape."""
    out = outline("auth.py", root=indexed_root)
    assert "file" in out
    assert "symbols" in out
    assert "files" not in out  # not directory mode


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
