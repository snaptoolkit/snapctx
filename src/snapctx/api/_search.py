"""``search_code`` — ranked search over indexed symbols.

Three modes (lexical / vector / hybrid). The actual ranking machinery
lives in ``_ranking``; this module just orchestrates: open the index,
run one or both backends, fuse results, format the response.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from snapctx.api._aliases import resolve_referenced_constants
from snapctx.api._common import (
    docstring_summary,
    open_index,
    row_to_symbol_dict,
)
from snapctx.api._ranking import (
    _exact_name_token,
    _simple_name,
    build_fts_query,
    classify_query,
    hybrid_weights,
    rrf_merge,
    search_hint,
    suggest_next_action,
)


def search_code(
    query: str,
    k: int = 5,
    kind: Literal["function", "method", "class", "module", "interface", "type", "component", "constant"] | None = None,
    root: str | Path = ".",
    mode: Literal["lexical", "vector", "hybrid"] = "hybrid",
    scope: str | None = None,
    with_bodies: bool = False,
    body_char_cap: int = 1500,
    also: tuple[str, ...] | list[str] | None = None,
) -> dict:
    """Find symbols whose qname, signature, docstring, or decorators match ``query``.

    ``mode`` selects the ranker:
      * ``lexical`` — SQLite FTS5 / BM25. Fast, exact-keyword; misses paraphrase.
      * ``vector``  — cosine similarity over bge-small embeddings of qname +
        signature + docstring. Best for paraphrased / conceptual queries.
      * ``hybrid``  — runs both, fuses ranks with Reciprocal Rank Fusion (k=60).
        Default. Robust: keeps lexical wins while recovering paraphrase hits.

    The response lists up to ``k`` hits, each with qualified name, one-line
    docstring summary, signature, file path + line range, score, and a
    suggested ``next_action`` the caller should take (``expand`` the call graph
    around a seed, ``read_body`` if signature+docstring look insufficient, or
    ``enough`` when the docstring alone is self-explanatory).

    ``with_bodies=True`` inlines each hit's source body (capped at
    ``body_char_cap`` chars per hit) so audit-style queries — "list every X
    that does Y" — can land all the source they need in one call instead of
    chasing each result with a follow-up ``get_source`` call. Pair with a
    higher ``k`` (e.g. ``k=20``) to overfetch.

    ``also=[...]`` runs the search across multiple terms in one call and
    unions the results. Use it for cross-cutting audits where the targets
    have multiple keywords — ``search("anthropic", also=["openai", "gemini"])``
    in one call replaces three separate searches and three LLM round-trips.
    Top-``k`` is applied to the merged-and-deduped result set, so a single
    call can comfortably cover a ``-k 30`` audit across half a dozen terms.
    """
    root_path = Path(root).resolve()
    queries: list[str] = [query] + list(also or [])
    idx = open_index(root_path, scope=scope)
    kind_filter_dropped = False
    actual_kinds: list[str] = []
    try:
        # Per-term ranked pairs, then merge-by-best-score across terms so
        # one call can serve ``audit "X" --also Y --also Z`` natively.
        per_term_pairs: list[list[tuple]] = []
        for q in queries:
            per_term_pairs.append(
                _rank_one(idx, q, k=k, kind=kind, mode=mode)
            )
        if len(per_term_pairs) == 1:
            pairs = per_term_pairs[0]
        else:
            pairs = _merge_pairs(per_term_pairs, k=k)

        # Common agent failure: pick wrong kind ("function" for what's
        # really a class method, "type" for what's an interface). Without
        # a retry, search returns nothing and the agent re-queries blind.
        # On an empty result with kind set, retry without it and surface
        # the actual kinds in the hint so the agent knows what to filter
        # next time.
        if kind is not None and not pairs:
            per_term_pairs = []
            for q in queries:
                per_term_pairs.append(
                    _rank_one(idx, q, k=k, kind=None, mode=mode)
                )
            if len(per_term_pairs) == 1:
                pairs = per_term_pairs[0]
            else:
                pairs = _merge_pairs(per_term_pairs, k=k)
            if pairs:
                kind_filter_dropped = True
                actual_kinds = sorted({row["kind"] for row, _ in pairs})

        results = []
        for row, score in pairs:
            d = row_to_symbol_dict(row)
            d["docstring"] = docstring_summary(row["docstring"])
            d["score"] = round(float(score), 4)
            d["next_action"] = suggest_next_action(row)
            if with_bodies:
                body = _read_body(row, body_char_cap)
                if body is not None:
                    d["source"] = body
                    # Inline literal values of any SCREAMING_SNAKE constants
                    # referenced in the body. Saves the agent a follow-up
                    # round-trip per ``DEFAULT_*_MODEL`` reference on audits.
                    consts = resolve_referenced_constants(
                        idx, body, exclude_qname=row["qname"],
                    )
                    if consts:
                        d["referenced_constants"] = consts
            results.append(d)

        # Stronger kind-mismatch detection than the empty-result retry: even
        # when ``kind`` returned hits, those hits may all be semantically-
        # related-but-wrong drift (e.g. ``kind='function'`` for what's really
        # a class method ``foo`` returns the package's ``foo_helper`` /
        # ``foo_api`` instead of the actual ``foo`` method). When the query
        # is a single identifier and *none* of the returned hits have the
        # exact simple-name match, but a symbol with that simple name exists
        # in another kind, surface it.
        kind_suggestion = (
            _detect_kind_suggestion(idx, query, kind, results) if kind is not None else None
        )
    finally:
        idx.close()

    if kind_filter_dropped:
        kinds_str = ", ".join(repr(k) for k in actual_kinds)
        hint = (
            f"No results with kind={kind!r}. Returning matches with other "
            f"kinds: {kinds_str}. In Python, class members are kind='method' "
            f"(not 'function'); in TypeScript, exported types are 'type' or "
            f"'interface' (not 'class')."
        )
    elif kind_suggestion:
        sug_qname, sug_kind = kind_suggestion
        hint = (
            f"Returned {len(results)} match(es) but none have the exact name "
            f"{_simple_name(sug_qname)!r}. A symbol with that exact name "
            f"exists as kind={sug_kind!r}: {sug_qname}. Re-run with "
            f"kind={sug_kind!r} to target it directly, or call "
            f"get_source({sug_qname!r}) to skip search entirely."
        )
    else:
        hint = search_hint(
            results, query=query, with_bodies=with_bodies, also_used=bool(also),
            kind_filter=kind,
        )

    response: dict = {
        "query": query,
        "mode": mode,
        "results": results,
        "hint": hint,
    }
    if kind_filter_dropped:
        response["kind_filter_dropped"] = True
        response["actual_kinds"] = actual_kinds
    if kind_suggestion:
        response["kind_suggestion"] = {
            "qname": kind_suggestion[0],
            "kind": kind_suggestion[1],
        }
    if also:
        response["also"] = list(also)
    if scope is not None:
        response["scope"] = scope
    return response


def _detect_kind_suggestion(
    idx, query: str, kind: str | None, results: list[dict]
) -> tuple[str, str] | None:
    """When a kinded search drifts to wrong-but-related results, find the
    canonical symbol the user probably meant and report its actual kind.

    Triggers only when:
      1. ``query`` is a single identifier-shaped token (so we can do a
         meaningful exact-name lookup — multi-word queries don't have a
         "the" simple name to match).
      2. None of the returned ``results`` have a simple_name equal to that
         token (i.e. all hits are drift, not exact matches).
      3. A symbol with that simple_name exists in the index under a
         *different* kind than the one filtered for.

    Returns ``(qname, kind)`` of the suggestion, or ``None`` when any of
    the above conditions don't hold. Limited to one suggestion — multiple
    same-name matches across kinds are too ambiguous to nudge confidently.
    """
    name = _exact_name_token(query)
    if name is None:
        return None
    # If any result already has the exact simple name, the user got what
    # they asked for; no suggestion needed.
    for r in results:
        if _simple_name(r["qname"]) == name:
            return None
    # Look up symbols whose qname ends with the simple-name segment.
    # ``%:name`` matches top-level symbols (``module:name``); ``%.name``
    # matches members (``module:Class.name``). Cap at a handful — if the
    # name is genuinely ambiguous, we don't want to nudge.
    rows = idx.conn.execute(
        "SELECT qname, kind FROM symbols WHERE qname LIKE ? OR qname LIKE ? "
        "LIMIT 3",
        (f"%:{name}", f"%.{name}"),
    ).fetchall()
    candidates = [
        (row["qname"], row["kind"])
        for row in rows
        if row["kind"] != kind and _simple_name(row["qname"]) == name
    ]
    if len(candidates) != 1:
        return None
    return candidates[0]


def _rank_one(idx, query: str, *, k: int, kind, mode: str) -> list[tuple]:
    """Rank one term against the index. Pulled out so multi-term batch
    can call this in a loop without duplicating the lexical/vector/hybrid
    branching."""
    fts_query = build_fts_query(query)
    overfetch = k * 3
    lex_rows = (
        idx.fts_search(fts_query, limit=overfetch, kind=kind)
        if mode != "vector" else []
    )
    vec_pairs: list[tuple] = []
    if mode in ("vector", "hybrid"):
        from snapctx.embeddings import embed_texts
        qvec = embed_texts([query])[0]
        vec_pairs = idx.vector_search(qvec, limit=overfetch, kind=kind)

    if mode == "lexical":
        return [(r, -float(r["score"])) for r in lex_rows][:k]
    if mode == "vector":
        return vec_pairs[:k]
    # hybrid
    lex_pairs = [(r, -float(r["score"])) for r in lex_rows]
    lw, vw = hybrid_weights(classify_query(query))
    return rrf_merge(
        lex_pairs, vec_pairs, limit=k, lex_weight=lw, vec_weight=vw, query=query,
    )


def _merge_pairs(per_term_pairs: list[list[tuple]], *, k: int) -> list[tuple]:
    """Merge per-term ranked pairs into a single top-K. Dedupes by qname,
    keeps each symbol's best score across terms (so a hit ranked 2 for
    ``anthropic`` and ranked 5 for ``api`` is treated as score-2)."""
    best: dict[str, tuple] = {}
    for pairs in per_term_pairs:
        for row, score in pairs:
            qname = row["qname"]
            if qname not in best or score > best[qname][1]:
                best[qname] = (row, score)
    merged = list(best.values())
    merged.sort(key=lambda x: -float(x[1]))
    return merged[:k]


def _read_body(row, body_char_cap: int) -> str | None:
    """Read a symbol's source slice from its file, capped at ``body_char_cap``.

    Returns ``None`` when the file is unreadable so the response stays
    JSON-clean. Slicing by stored line range avoids re-parsing.
    """
    try:
        text = Path(row["file"]).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    lines = text.splitlines()
    start = max(1, int(row["line_start"]))
    end = max(start, int(row["line_end"]))
    body = "\n".join(lines[start - 1 : end])
    if len(body) > body_char_cap:
        body = body[:body_char_cap] + (
            f"\n# ... truncated ({len(body) - body_char_cap} chars) ..."
        )
    return body
