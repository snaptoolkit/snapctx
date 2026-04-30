from __future__ import annotations

import sqlite3
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


def test_index_root_force_reindexes_unchanged_files(tmp_path: Path) -> None:
    """`force=True` rebuilds the index from scratch even when SHAs match.

    Needed after a parser upgrade: a file whose bytes haven't changed still
    needs to be re-parsed so newly-emitted symbol kinds land in the index.
    Without ``force``, the SHA-keyed incremental skip would silently keep
    the old (now stale) parse output.
    """
    from snapctx.api import index_root

    root = tmp_path / "repo"
    root.mkdir()
    (root / "a.py").write_text("def foo(): return 1\n")
    summary1 = index_root(root)
    assert summary1["files_updated"] == 1

    # Same bytes — incremental call skips the file entirely.
    summary2 = index_root(root)
    assert summary2["files_updated"] == 0
    assert summary2["files_unchanged"] == 1

    # `force=True` re-parses despite SHA match.
    summary3 = index_root(root, force=True)
    assert summary3["files_updated"] == 1
    assert summary3["files_unchanged"] == 0


def test_rrf_docs_penalty_demotes_doc_seeds_when_code_seeds_exist() -> None:
    """Real-world bias from agent-on-biblereader: a "how does X work" query
    pulled README headings above the implementation because the HTML/Markdown
    parsers index prose. When the candidate pool contains at least one
    code-language seed, demote markdown/html/text seeds.
    """
    from snapctx.api._ranking import rrf_merge

    def row(qname: str, file: str, language: str) -> dict:
        return {"qname": qname, "file": file, "language": language}

    # Without the penalty, the doc would win because both lists rank it #1.
    lex = [
        (row("docs/translation-pipeline.md:Pipeline", "/r/docs/p.md", "markdown"), -5.0),
        (row("backend.commands.translate:run_translation", "/r/backend/c.py", "python"), -3.0),
    ]
    vec = [
        (row("docs/translation-pipeline.md:Pipeline", "/r/docs/p.md", "markdown"), 0.78),
        (row("backend.commands.translate:run_translation", "/r/backend/c.py", "python"), 0.72),
    ]
    merged = rrf_merge(lex, vec, limit=2, query="how does the translation pipeline work")
    assert merged[0][0]["qname"] == "backend.commands.translate:run_translation"


def test_rrf_docs_penalty_off_when_no_code_seeds_present() -> None:
    """Doc-only queries (e.g. searching across a docs-only repo) must not be
    penalized — there's nothing to demote them in favor of."""
    from snapctx.api._ranking import rrf_merge

    def row(qname: str, language: str) -> dict:
        return {"qname": qname, "file": "/r/docs/x.md", "language": language}

    lex = [(row("docs/install.md:Install", "markdown"), -5.0), (row("docs/setup.md:Setup", "markdown"), -3.0)]
    vec = [(row("docs/install.md:Install", "markdown"), 0.82), (row("docs/setup.md:Setup", "markdown"), 0.71)]
    merged = rrf_merge(lex, vec, limit=2, query="install guide")
    # Both are docs — top result is the lex+vec winner regardless of language.
    assert merged[0][0]["qname"] == "docs/install.md:Install"


def test_rrf_docs_penalty_off_for_identifier_queries() -> None:
    """Identifier queries (single token like ``run_translation``) must not
    demote docs — the user is asking by name, and a docs heading literally
    titled ``run_translation`` is a legitimate hit."""
    from snapctx.api._ranking import rrf_merge

    def row(qname: str, language: str) -> dict:
        return {"qname": qname, "file": "/r/x.py" if language == "python" else "/r/d.md",
                "language": language}

    lex = [
        (row("docs/api.md:run_translation", "markdown"), -1.0),  # very strong doc match
        (row("backend.cmd:run_translation", "python"), -3.0),
    ]
    vec = [
        (row("docs/api.md:run_translation", "markdown"), 0.85),
        (row("backend.cmd:run_translation", "python"), 0.70),
    ]
    merged = rrf_merge(lex, vec, limit=2, query="run_translation")
    # Identifier query → docs penalty does NOT fire; docs wins because lex+vec
    # both rank it first. (The name-match bonus is symmetric.)
    assert merged[0][0]["qname"] == "docs/api.md:run_translation"


def test_rrf_docs_penalty_handles_rows_missing_language() -> None:
    """Older mock rows / non-symbol rows might not carry a 'language' key —
    don't crash, just skip the demotion check for those."""
    from snapctx.api._ranking import rrf_merge

    def row(qname: str) -> dict:
        return {"qname": qname, "file": "/x.py"}  # no 'language'

    lex = [(row("a:foo"), -3.0), (row("a:bar"), -2.0)]
    vec = [(row("a:bar"), 0.7), (row("a:foo"), 0.6)]
    merged = rrf_merge(lex, vec, limit=2, query="some natural language query here please")
    # No crash; just a normal merge.
    assert {r["qname"] for r, _ in merged} == {"a:foo", "a:bar"}


def test_search_with_wrong_kind_retries_and_hints(tmp_path: Path) -> None:
    """Common failure mode: agent passes ``kind='function'`` for a Python
    class method (which is actually ``kind='method'``). Search should
    retry without the kind filter and hint at the actual kinds available.
    """
    from snapctx.api import index_root, search_code

    root = tmp_path / "repo"
    root.mkdir()
    (root / "models.py").write_text(
        "class User:\n"
        "    def get_translation_instructions(self):\n"
        "        return 'hello'\n"
    )
    index_root(root)

    out = search_code("get_translation_instructions", kind="function", root=root, mode="lexical")
    assert out["results"], "retry should surface the method"
    assert out.get("kind_filter_dropped") is True
    assert "method" in out.get("actual_kinds", [])
    assert "method" in out["hint"].lower()
    assert "no results with kind='function'" in out["hint"].lower()


def test_search_with_correct_kind_does_not_trigger_retry(tmp_path: Path) -> None:
    """When the kind filter matches at least one symbol, no retry — the
    response shape stays unchanged for callers that depend on it."""
    from snapctx.api import index_root, search_code

    root = tmp_path / "repo"
    root.mkdir()
    (root / "x.py").write_text(
        "class C:\n"
        "    def get_translation_instructions(self):\n"
        "        return 1\n"
    )
    index_root(root)

    out = search_code("get_translation_instructions", kind="method", root=root, mode="lexical")
    assert out["results"]
    assert "kind_filter_dropped" not in out
    assert "actual_kinds" not in out


def test_search_with_no_kind_and_no_results_returns_empty_normally(tmp_path: Path) -> None:
    """No kind filter, no hits → don't fabricate a retry message; just the
    standard "no matches" hint."""
    from snapctx.api import index_root, search_code

    root = tmp_path / "repo"
    root.mkdir()
    (root / "x.py").write_text("def f(): return 1\n")
    index_root(root)

    out = search_code("nonexistent_thing_xyz", root=root, mode="lexical")
    assert out["results"] == []
    assert "kind_filter_dropped" not in out


def test_search_surfaces_kind_suggestion_when_wrong_kind_drifts(tmp_path: Path) -> None:
    """The harder failure case: kind filter returns *some* results but they're
    all semantically-related-but-wrong drift, while a same-name symbol
    actually exists in a different kind. Surface that with a hint and a
    structured ``kind_suggestion`` field.
    """
    from snapctx.api import index_root, search_code

    root = tmp_path / "repo"
    root.mkdir()
    (root / "models.py").write_text(
        "class Translator:\n"
        "    def get_translation_verse_word_mapping(self):\n"
        "        return {}\n"
    )
    (root / "phases.py").write_text(
        "def run_word_mappings_phase():\n"
        "    return None\n"
        "def word_mapping_api():\n"
        "    return None\n"
    )
    index_root(root)

    # Hybrid (default) reproduces the agent's reported drift: the embedding
    # model finds functions whose names are semantically related to the
    # query (``word_mapping_api``, ``run_word_mappings_phase``) and lets
    # them through the kind=function filter — even though the user
    # actually wanted the exact-name method.
    out = search_code(
        "get_translation_verse_word_mapping",
        kind="function", root=root, mode="hybrid",
    )
    assert out["results"], "drift hits should appear with kind=function"
    assert out.get("kind_suggestion") is not None
    assert out["kind_suggestion"]["kind"] == "method"
    assert out["kind_suggestion"]["qname"].endswith(":Translator.get_translation_verse_word_mapping")
    assert "method" in out["hint"].lower()


def test_search_no_kind_suggestion_when_exact_match_present(tmp_path: Path) -> None:
    """When the query's simple name appears in the results, no suggestion —
    the user got what they asked for, no nudge needed."""
    from snapctx.api import index_root, search_code

    root = tmp_path / "repo"
    root.mkdir()
    (root / "x.py").write_text("def get_thing(): return 1\n")
    (root / "y.py").write_text("def get_thing_helper(): return 2\n")
    index_root(root)

    out = search_code("get_thing", kind="function", root=root, mode="lexical")
    assert any(r["qname"].endswith(":get_thing") for r in out["results"])
    assert "kind_suggestion" not in out


def test_search_no_kind_suggestion_for_multi_word_query(tmp_path: Path) -> None:
    """Multi-word queries don't have a single simple name to match — no
    kind suggestion, just normal results."""
    from snapctx.api import index_root, search_code

    root = tmp_path / "repo"
    root.mkdir()
    (root / "a.py").write_text("def something(): return 1\n")
    (root / "b.py").write_text(
        "class Other:\n"
        "    def do_work(self): return 2\n"
    )
    index_root(root)

    out = search_code("do work please", kind="function", root=root, mode="lexical")
    assert "kind_suggestion" not in out


def test_search_no_kind_suggestion_when_multiple_kinds_share_name(tmp_path: Path) -> None:
    """If the same simple name exists in multiple kinds, the suggestion is
    ambiguous — don't nudge in that case."""
    from snapctx.api import index_root, search_code

    root = tmp_path / "repo"
    root.mkdir()
    (root / "a.py").write_text("def login(): return 1\n")
    (root / "b.py").write_text(
        "class User:\n"
        "    def login(self): return 2\n"
    )
    (root / "c.py").write_text(
        "class Admin:\n"
        "    def login(self): return 3\n"
    )
    index_root(root)

    out = search_code("xyz_unrelated_to_anything", kind="function", root=root, mode="lexical")
    # Two methods named login exist, but query doesn't match either.
    # No kind_suggestion because multiple kinds match.
    assert "kind_suggestion" not in out


def test_path_hint_boosts_matching_files() -> None:
    """When the query carries a path-shape token, files matching that path
    rank above otherwise-similar files elsewhere in the repo. Lets the agent
    say "tell me about routing in frontend/i18n" and have the ranker
    actually listen, rather than returning generic ``routing`` matches from
    backend or docs.
    """
    from snapctx.api._ranking import rrf_merge

    def row(qname: str, file: str, language: str = "typescript") -> dict:
        return {"qname": qname, "file": file, "language": language}

    # Both files match the term ``routing`` in lex+vec; without the hint
    # the ranking is a tie. With ``frontend/i18n`` in the query, the
    # frontend file wins.
    matching = row("frontend/i18n/routing:defineRouting", "/r/frontend/i18n/routing.ts")
    other = row("backend.routes:setup", "/r/backend/routes.py", "python")
    lex = [(matching, -3.0), (other, -3.0)]
    vec = [(matching, 0.7), (other, 0.7)]

    no_hint = rrf_merge(lex, vec, limit=2, query="routing setup")
    # Tied — order is preserved from lexical first-seen.
    assert {r["qname"] for r, _ in no_hint} == {matching["qname"], other["qname"]}

    with_hint = rrf_merge(lex, vec, limit=2, query="routing setup frontend/i18n")
    assert with_hint[0][0]["qname"] == matching["qname"]


def test_path_hint_boost_accepts_sqlite_rows() -> None:
    """Production search results are ``sqlite3.Row`` objects, not plain dicts."""
    from snapctx.api._ranking import rrf_merge

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT ? AS qname, ? AS file, ? AS language",
            (
                "frontend/i18n/routing:defineRouting",
                "/r/frontend/i18n/routing.ts",
                "typescript",
            ),
        ).fetchone()
    finally:
        conn.close()

    assert row is not None
    merged = rrf_merge([(row, -1.0)], [(row, 0.8)], limit=1, query="routing setup frontend/i18n")
    assert merged[0][0]["qname"] == "frontend/i18n/routing:defineRouting"


def test_path_hint_extractor_accepts_real_paths_rejects_garbage() -> None:
    from snapctx.api._ranking import _path_hints

    assert _path_hints("how does routing work in frontend/i18n") == ["frontend/i18n"]
    assert _path_hints("backend/parser stuff") == ["backend/parser"]
    assert _path_hints("a/b") == []                  # segments too short
    assert _path_hints("https://example.com/x") == []  # URL
    assert _path_hints("just words no path") == []
    # Trailing punctuation tolerated.
    assert _path_hints("(see frontend/i18n)") == ["frontend/i18n"]


def test_docs_penalty_now_demotes_json_modules_on_natural_query() -> None:
    """Real biblereader case: a query like "navigation locale request"
    matched ``messages/el.json`` keys above ``frontend/i18n/routing.ts``.
    Extending DOC_LANGUAGES to include JSON/YAML/TOML demotes those
    config keys when code seeds exist in the pool."""
    from snapctx.api._ranking import rrf_merge

    code = {"qname": "frontend/i18n/routing:defineRouting", "file": "/r/frontend/i18n/routing.ts", "language": "typescript"}
    config_key = {"qname": "messages/el.json:navigation", "file": "/r/frontend/messages/el.json", "language": "json"}

    # JSON ranks first in both before docs penalty.
    lex = [(config_key, -1.0), (code, -3.0)]
    vec = [(config_key, 0.85), (code, 0.70)]
    merged = rrf_merge(lex, vec, limit=2, query="how does navigation locale request work")
    assert merged[0][0]["qname"] == code["qname"]
