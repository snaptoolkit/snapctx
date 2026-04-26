"""``search_code`` — ranked search over indexed symbols.

Three modes (lexical / vector / hybrid). The actual ranking machinery
lives in ``_ranking``; this module just orchestrates: open the index,
run one or both backends, fuse results, format the response.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from snapctx.api._common import (
    docstring_summary,
    open_index,
    row_to_symbol_dict,
)
from snapctx.api._ranking import (
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
    """
    root_path = Path(root).resolve()
    idx = open_index(root_path, scope=scope)
    try:
        fts_query = build_fts_query(query)
        overfetch = k * 3
        lex_rows = idx.fts_search(fts_query, limit=overfetch, kind=kind) if mode != "vector" else []

        vec_pairs: list[tuple] = []
        if mode in ("vector", "hybrid"):
            from snapctx.embeddings import embed_texts
            qvec = embed_texts([query])[0]
            vec_pairs = idx.vector_search(qvec, limit=overfetch, kind=kind)

        if mode == "lexical":
            pairs = [(r, -float(r["score"])) for r in lex_rows][:k]
        elif mode == "vector":
            pairs = vec_pairs[:k]
        else:  # hybrid
            lex_pairs = [(r, -float(r["score"])) for r in lex_rows]
            lw, vw = hybrid_weights(classify_query(query))
            pairs = rrf_merge(
                lex_pairs, vec_pairs, limit=k, lex_weight=lw, vec_weight=vw, query=query
            )
    finally:
        idx.close()

    results = []
    for row, score in pairs:
        d = row_to_symbol_dict(row)
        d["docstring"] = docstring_summary(row["docstring"])
        d["score"] = round(float(score), 4)
        d["next_action"] = suggest_next_action(row)
        results.append(d)

    response: dict = {
        "query": query,
        "mode": mode,
        "results": results,
        "hint": search_hint(results),
    }
    if scope is not None:
        response["scope"] = scope
    return response
