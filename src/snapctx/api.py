"""The four context-retrieval operations on top of a populated index.

Every function takes a repo root (Path) and returns a JSON-serializable dict.
The CLI wraps these directly.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Literal

from snapctx.index import Index, db_path_for

# A response chunk roughly this size — used to decide when to truncate.
DEFAULT_RESULT_BUDGET = 2000   # tokens, approximate (4 chars ≈ 1 token)


# ---------- helpers ----------


def _open(root: Path) -> Index:
    db = db_path_for(root)
    if not db.exists():
        raise FileNotFoundError(
            f"No index at {db}. Run `snapctx index {root}` first."
        )
    return Index(db)


def _row_to_symbol_dict(row: sqlite3.Row, *, include_body_line_range: bool = True) -> dict:
    d = {
        "qname": row["qname"],
        "kind": row["kind"],
        "language": row["language"],
        "signature": row["signature"],
        "docstring": row["docstring"],
        "file": row["file"],
        "parent_qname": row["parent_qname"],
    }
    if include_body_line_range:
        d["lines"] = f"{row['line_start']}-{row['line_end']}"
    if row["decorators"]:
        d["decorators"] = row["decorators"].split("\n")
    return d


def _docstring_summary(docstring: str | None) -> str | None:
    """Return just the first sentence/line of a docstring — sized for search results."""
    if not docstring:
        return None
    first_line = docstring.strip().splitlines()[0]
    return first_line


def _build_fts_query(user_query: str) -> str:
    """Map a natural-language-ish query into FTS5 MATCH syntax.

    Splits the input into bare tokens and ORs them, so a multi-word query
    matches any of the terms. FTS5's own tokenizer handles further normalization.
    """
    tokens = [t for t in _tokenize_query(user_query) if t]
    if not tokens:
        return user_query
    return " OR ".join(tokens)


def _tokenize_query(q: str) -> list[str]:
    import re
    return [t for t in re.findall(r"\w+", q.lower()) if t]


# Words that strongly signal a query is English prose rather than a symbol
# lookup. Used by ``_classify_query`` to pick ranker weights — not stripped
# from the query itself. We're conservative: only include very common "wh-"
# words, auxiliaries, and a handful of prepositions. A snake_case identifier
# like ``user_is_active`` contains "is" but we care about token-level matches,
# not substring.
_NL_STOPWORDS = frozenset({
    "how", "what", "why", "where", "when", "which", "who", "whose",
    "does", "do", "did", "is", "are", "was", "were", "be", "been", "being",
    "the", "a", "an", "and", "or", "of", "for", "to", "in", "on", "at",
    "from", "with", "by", "about", "after", "before", "between", "into",
    "through", "via", "over", "under", "that", "this", "these", "those",
})


_CAMEL_RE = __import__("re").compile(r"[a-z][A-Z]")


def _looks_like_identifier(token: str) -> bool:
    """Heuristic: does this token look like a source-code identifier?"""
    if not token:
        return False
    if any(c in token for c in "._:/"):
        return True          # dotted / qname-ish
    if "_" in token and token.replace("_", "").isalnum():
        return True          # snake_case
    if token.isupper() and len(token) >= 2:
        return True          # CONSTANT_CASE
    if _CAMEL_RE.search(token):
        return True          # camelCase / PascalCase
    return False


def _classify_query(query: str) -> str:
    """Return 'identifier' | 'natural' | 'mixed'.

    - ``identifier``: ≤ 2 tokens AND at least one looks like a source
      identifier (camelCase, snake_case, dotted, or qname).
    - ``natural``: 5+ tokens with at least one English stopword, OR 4+ tokens
      with 2+ stopwords.
    - ``mixed``: everything else (short freeform like "rate limit", or
      medium-length hybrid queries).
    """
    raw_tokens = query.split()
    tokens = _tokenize_query(query)
    if not raw_tokens:
        return "mixed"
    # Identifier lookup: a dotted qname like ``apps.auth:login`` is a single
    # raw word even though it contains multiple ``\w+`` matches, so count
    # whitespace-split words here.
    if len(raw_tokens) <= 2 and any(_looks_like_identifier(t) for t in raw_tokens):
        return "identifier"
    n_stop = sum(1 for t in tokens if t in _NL_STOPWORDS)
    if (len(tokens) >= 5 and n_stop >= 1) or (len(tokens) >= 4 and n_stop >= 2):
        return "natural"
    return "mixed"


# ---------- search_code ----------


def search_code(
    query: str,
    k: int = 5,
    kind: Literal["function", "method", "class", "module", "interface", "type", "component", "constant"] | None = None,
    root: str | Path = ".",
    mode: Literal["lexical", "vector", "hybrid"] = "hybrid",
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
    root = Path(root).resolve()
    idx = _open(root)
    try:
        fts_query = _build_fts_query(query)
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
            # Adapt RRF weights to query style. "How does the frontend …" is
            # natural prose — trust the embedding model. "run_exscript" is an
            # identifier lookup — BM25 nails it exactly. A short freeform
            # "rate limit" falls in the middle.
            qclass = _classify_query(query)
            if qclass == "natural":
                lw, vw = 0.5, 2.5
            elif qclass == "identifier":
                lw, vw = 1.5, 0.8
            else:
                lw, vw = 1.0, 1.5
            pairs = _rrf_merge(
                lex_pairs, vec_pairs, limit=k, lex_weight=lw, vec_weight=vw,
            )
    finally:
        idx.close()

    results = []
    for row, score in pairs:
        d = _row_to_symbol_dict(row)
        d["docstring"] = _docstring_summary(row["docstring"])
        d["score"] = round(float(score), 4)
        d["next_action"] = _suggest_next_action(row)
        results.append(d)

    return {
        "query": query,
        "mode": mode,
        "results": results,
        "hint": _search_hint(results),
    }


def _rrf_merge(
    lexical_pairs,
    vector_pairs,
    *,
    k_fuse: int = 60,
    limit: int = 5,
    lex_weight: float = 1.0,
    vec_weight: float = 1.5,
    test_penalty: float = 0.6,
):
    """Weighted Reciprocal Rank Fusion of two ranked lists of (row, score) tuples.

    Defaults (tuned empirically on the biblereader benchmark):
      * ``vec_weight=1.5`` — embeddings beat BM25 on identifier-heavy queries
        because BM25's camel/snake token splits mix real matches with noise.
      * ``test_penalty=0.6`` — symbols living under a ``tests/`` path or in a
        module ending in ``.tests`` or starting with ``test_`` get their RRF
        score multiplied by this factor. Agents almost never want a test as
        their top hit, but tests still appear (just demoted).
    """
    scores: dict[str, float] = {}
    kept: dict[str, object] = {}
    for pairs, weight in ((lexical_pairs, lex_weight), (vector_pairs, vec_weight)):
        for rank, (row, _) in enumerate(pairs, start=1):
            q = row["qname"]
            scores[q] = scores.get(q, 0.0) + weight / (k_fuse + rank)
            kept[q] = row
    # Apply test-file penalty.
    for q, row in kept.items():
        if _looks_like_test(q, row["file"]):
            scores[q] *= test_penalty
    ordered = sorted(scores.items(), key=lambda kv: -kv[1])
    return [(kept[q], s) for q, s in ordered[:limit]]


def _looks_like_test(qname: str, file: str) -> bool:
    return (
        "/tests/" in file
        or "/test/" in file
        or file.endswith("/tests.py")
        or "tests:" in qname
        or ":Test" in qname
        or qname.endswith(".tests")
    )


def _suggest_next_action(row: sqlite3.Row) -> str:
    if row["kind"] in ("class", "module"):
        return "outline"
    if row["docstring"] and len(row["docstring"]) > 40:
        return "expand"
    return "read_body"


def _search_hint(results: list[dict]) -> str:
    if not results:
        return (
            "No matches. Try synonyms (e.g. 'throttle' instead of 'rate limit'), "
            "or a different `kind` filter."
        )
    top = results[0]
    qname = top["qname"]
    if top["next_action"] == "expand":
        return f"To see callees of the top result, call expand({qname!r})."
    if top["next_action"] == "outline":
        return f"To see {qname}'s members, call outline with its file."
    return f"If the signature isn't enough, call get_source({qname!r})."


# ---------- expand ----------


def expand(
    qname: str,
    direction: Literal["callees", "callers", "both"] = "callees",
    depth: int = 1,
    root: str | Path = ".",
) -> dict:
    """Walk the call graph from ``qname`` and return neighbor signatures.

    ``direction`` picks which edges to follow:
      - ``callees``: functions/methods that ``qname`` invokes.
      - ``callers``: functions/methods that invoke ``qname``.
      - ``both``: union of the two.

    ``depth`` controls how many hops. At depth 1 you get the immediate
    neighborhood; at depth 2 you also see what those neighbors call/are-called-by.
    Returns neighbor **signatures and docstring summaries** only — no bodies —
    so the caller can decide which ones (if any) need `get_source`.
    """
    root = Path(root).resolve()
    idx = _open(root)
    try:
        root_sym = idx.get_symbol(qname)
        if root_sym is None:
            return {
                "qname": qname,
                "error": "not_found",
                "hint": f"No symbol named {qname!r}. Call search_code first to find valid qnames.",
            }

        visited: set[str] = {qname}
        layers: list[list[dict]] = []
        frontier: list[str] = [qname]

        for hop in range(1, depth + 1):
            next_frontier: list[str] = []
            layer: list[dict] = []
            for source_qname in frontier:
                neighbors = _neighbors(idx, source_qname, direction)
                for neigh_qname, neigh_row, edge_kind, call_line in neighbors:
                    if neigh_qname in visited:
                        continue
                    visited.add(neigh_qname)
                    next_frontier.append(neigh_qname)
                    entry = {
                        "from": source_qname,
                        "edge": edge_kind,
                        "line": call_line,
                    }
                    if neigh_row is not None:
                        entry.update(_row_to_symbol_dict(neigh_row))
                        entry["docstring"] = _docstring_summary(neigh_row["docstring"])
                    else:
                        # Unresolved callee — name only.
                        entry["qname"] = neigh_qname
                        entry["resolved"] = False
                    layer.append(entry)
            layers.append(layer)
            frontier = next_frontier
            if not frontier:
                break

        return {
            "qname": qname,
            "root_signature": root_sym["signature"],
            "direction": direction,
            "depth": depth,
            "layers": layers,
            "hint": _expand_hint(layers),
        }
    finally:
        idx.close()


def _neighbors(
    idx: Index, qname: str, direction: str
) -> list[tuple[str, sqlite3.Row | None, str, int]]:
    """Return (neighbor_qname, neighbor_symbol_row_or_None, edge_kind, line) tuples."""
    out: list[tuple[str, sqlite3.Row | None, str, int]] = []
    if direction in ("callees", "both"):
        for row in idx.callees_of(qname):
            neigh_qname = row["callee_qname"] or f"?:{row['callee_name']}"
            sym = idx.get_symbol(neigh_qname) if row["callee_qname"] else None
            out.append((neigh_qname, sym, "callee", row["line"]))
    if direction in ("callers", "both"):
        for row in idx.callers_of(qname):
            neigh_qname = row["caller_qname"]
            sym = idx.get_symbol(neigh_qname)
            out.append((neigh_qname, sym, "caller", row["line"]))
    return out


def _expand_hint(layers: list[list[dict]]) -> str:
    total = sum(len(layer) for layer in layers)
    if total == 0:
        return "No neighbors found at the requested depth/direction."
    unresolved = sum(1 for layer in layers for e in layer if e.get("resolved") is False)
    if unresolved:
        return (
            f"{total} neighbors ({unresolved} unresolved — likely stdlib or dynamic calls). "
            "Call get_source on a resolved neighbor if you need its body."
        )
    return f"{total} neighbors. Call get_source on any one to see its body."


# ---------- outline ----------


def outline(path: str | Path, root: str | Path = ".") -> dict:
    """List all symbols defined in a file, nested by parent.

    Accepts an absolute path or a path relative to ``root``. Returns the file's
    symbol tree in source order, each node carrying its signature, one-line
    docstring summary, and line range. No bodies.

    Use this instead of reading a whole file when you only need to know what
    it defines — typically a 10x token savings over ``get_source`` of the file.
    """
    root = Path(root).resolve()
    target = Path(path)
    if not target.is_absolute():
        target = (root / target).resolve()
    file_str = str(target)

    idx = _open(root)
    try:
        rows = idx.symbols_in_file(file_str)
    finally:
        idx.close()

    if not rows:
        return {
            "file": file_str,
            "symbols": [],
            "hint": f"No symbols indexed for {file_str}. Did you run `snapctx index` on this root?",
        }

    by_qname = {row["qname"]: row for row in rows}
    tree = _nest_symbols(rows, by_qname)
    return {"file": file_str, "symbols": tree}


def _nest_symbols(rows: list[sqlite3.Row], by_qname: dict[str, sqlite3.Row]) -> list[dict]:
    """Build a tree from a flat list of Symbols ordered by line_start.

    A symbol's children are the symbols whose parent_qname is this symbol's qname.
    """
    children_of: dict[str | None, list[sqlite3.Row]] = {}
    for row in rows:
        children_of.setdefault(row["parent_qname"], []).append(row)

    def build(row: sqlite3.Row) -> dict:
        d = _row_to_symbol_dict(row)
        d["docstring"] = _docstring_summary(row["docstring"])
        kids = children_of.get(row["qname"], [])
        if kids:
            d["children"] = [build(k) for k in kids]
        return d

    # Roots are rows whose parent_qname is None, or whose parent isn't in this file.
    roots = [r for r in rows if r["parent_qname"] is None or r["parent_qname"] not in by_qname]
    return [build(r) for r in roots]


# ---------- get_source ----------


def get_source(
    qname: str,
    with_neighbors: bool = False,
    root: str | Path = ".",
) -> dict:
    """Return the full source of a symbol, and optionally the signatures of what it calls.

    ``with_neighbors=True`` appends a compact list of this symbol's resolved
    callees (signature + docstring summary only), so the caller can reason
    about the dependency context without a follow-up round-trip.
    """
    root = Path(root).resolve()
    idx = _open(root)
    try:
        row = idx.get_symbol(qname)
        if row is None:
            return {
                "qname": qname,
                "error": "not_found",
                "hint": f"No symbol {qname!r} in index.",
            }

        path = Path(row["file"])
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as e:
            return {"qname": qname, "error": f"read_failed: {e}"}

        body = "\n".join(lines[row["line_start"] - 1 : row["line_end"]])

        result = {
            "qname": qname,
            "signature": row["signature"],
            "file": row["file"],
            "lines": f"{row['line_start']}-{row['line_end']}",
            "source": body,
        }

        if with_neighbors:
            callees = []
            for call_row in idx.callees_of(qname):
                if not call_row["callee_qname"]:
                    continue
                neigh = idx.get_symbol(call_row["callee_qname"])
                if neigh is None:
                    continue
                callees.append(
                    {
                        "qname": neigh["qname"],
                        "signature": neigh["signature"],
                        "docstring": _docstring_summary(neigh["docstring"]),
                    }
                )
            result["callees"] = callees

        return result
    finally:
        idx.close()


# ---------- context: one-shot retrieval ----------


def context(
    query: str,
    *,
    k_seeds: int = 5,
    source_for_top: int = 5,
    expand_depth: int = 2,
    neighbor_limit: int = 8,
    body_char_cap: int = 2000,
    file_outline_limit: int = 8,
    outline_discovery_k: int = 15,
    mode: Literal["lexical", "vector", "hybrid"] = "hybrid",
    kind: str | None = None,
    root: str | Path = ".",
) -> dict:
    """Gather everything an agent needs about ``query`` in one call.

    Runs ``search_code`` → ``expand(both)`` → ``get_source`` under the hood and
    returns a single self-contained payload. Designed for agents that want to
    minimize tool-call round trips at the cost of some extra tokens.

    Fast paths:
      * If ``query`` is an exact qname (contains ``:`` and matches a known
        symbol), the search step is skipped — we go straight to building the
        pack around that single symbol.

    For each of the top ``k_seeds`` search hits, the response includes:
      - qname, signature, docstring, file, line range
      - up to ``neighbor_limit`` callees (signature + docstring summary)
      - up to ``neighbor_limit`` callers (signature + docstring summary)
      - full source code for the top ``source_for_top`` seeds
      - **resolved_value** for constant aliases (e.g. ``NAME = OTHER_NAME`` is
        followed up to 3 hops so the agent sees the terminal literal).

    File outlines: the search is overfetched to ``outline_discovery_k``
    candidates (15 by default). We return only the top ``k_seeds`` as seeds,
    but use the broader candidate pool to discover up to
    ``file_outline_limit`` unique files to outline. This closes the "survey"
    gap on codebases with many small files (e.g. Next.js App Router page
    trees) where the top 5 seeds don't span the full relevant surface.
    """
    root_path = Path(root).resolve()
    candidates: list[dict] = []      # broader candidate pool (for file discovery)

    # Fast path: exact qname lookup, skipping the search pipeline entirely.
    if ":" in query:
        idx_tmp = _open(root_path)
        try:
            direct = idx_tmp.get_symbol(query)
        finally:
            idx_tmp.close()
        if direct is not None:
            seeds = [
                {
                    "qname": direct["qname"],
                    "kind": direct["kind"],
                    "signature": direct["signature"],
                    "docstring": _docstring_summary(direct["docstring"]),
                    "file": direct["file"],
                    "lines": f"{direct['line_start']}-{direct['line_end']}",
                    "score": 1.0,
                    "decorators": direct["decorators"].split("\n") if direct["decorators"] else None,
                }
            ]
            candidates = seeds
            mode = "exact"
        else:
            search_result = search_code(
                query, k=max(k_seeds, outline_discovery_k), kind=kind, root=root_path, mode=mode
            )
            candidates = search_result["results"]
            seeds = candidates[:k_seeds]
    else:
        search_result = search_code(
            query, k=max(k_seeds, outline_discovery_k), kind=kind, root=root_path, mode=mode
        )
        candidates = search_result["results"]
        seeds = candidates[:k_seeds]

    if not seeds:
        return {
            "query": query,
            "mode": mode,
            "seeds": [],
            "hint": "No matches. Try different keywords, or widen with a conceptual query (e.g. 'retry logic' vs 'ErrorHandler').",
            "token_estimate": 0,
        }

    idx = _open(root_path)
    try:
        enriched: list[dict] = []
        top_file_outline: dict | None = None

        for i, seed in enumerate(seeds):
            qname = seed["qname"]
            entry = {
                "rank": i + 1,
                "qname": qname,
                "kind": seed["kind"],
                "signature": seed["signature"],
                "docstring": seed.get("docstring"),
                "file": seed["file"],
                "lines": seed.get("lines"),
                "score": seed.get("score"),
            }
            if seed.get("decorators"):
                entry["decorators"] = seed["decorators"]

            # Neighborhood: callees + callers, walked to expand_depth so an
            # agent sees the call path (e.g. emit → _publish_delta →
            # client.publish) without a follow-up ``expand`` call.
            callees = _collect_neighbors(
                idx, qname, direction="callees",
                limit=neighbor_limit, depth=max(1, expand_depth),
            )
            callers = _collect_neighbors(
                idx, qname, direction="callers",
                limit=neighbor_limit, depth=max(1, expand_depth),
            )
            if callees:
                entry["callees"] = callees
            if callers:
                entry["callers"] = callers

            # Full source for the top ``source_for_top`` seeds.
            if i < source_for_top:
                try:
                    src = Path(seed["file"]).read_text(encoding="utf-8", errors="replace")
                    lines = src.splitlines()
                    start, end = _parse_line_range(seed.get("lines", "1-1"))
                    body = "\n".join(lines[start - 1 : end])
                    if len(body) > body_char_cap:
                        body = body[:body_char_cap] + f"\n# ... truncated ({len(body) - body_char_cap} chars) ..."
                    entry["source"] = body
                except OSError as e:
                    entry["source_error"] = str(e)

            # Constant-alias resolution: if this seed is `NAME = OTHER_NAME`,
            # follow the chain and attach the terminal literal value. Works
            # across files — the agent sees the real string without an extra
            # call.
            if seed["kind"] == "constant":
                resolved = _resolve_constant_chain(idx, seed["signature"], seed["qname"])
                if resolved is not None:
                    entry["resolved_value"] = resolved

            enriched.append(entry)

        # File outlines: walk the BROADER candidate pool (not just the top-K
        # seeds the agent sees) to discover unique files. This is how survey
        # questions — "list every route", "every agent class" — get coverage
        # in one call even when the top-K seeds cluster in a couple of files.
        seen_files: list[str] = []
        for cand in candidates:
            if cand["file"] not in seen_files:
                seen_files.append(cand["file"])
            if len(seen_files) >= file_outline_limit:
                break
        file_outlines: list[dict] = []
        for f in seen_files:
            rows = idx.symbols_in_file(f)
            if not rows:
                continue
            file_outlines.append(
                {
                    "file": f,
                    "symbols": [
                        {
                            "qname": r["qname"],
                            "kind": r["kind"],
                            "signature": r["signature"],
                            "lines": f"{r['line_start']}-{r['line_end']}",
                        }
                        for r in rows
                    ],
                }
            )
    finally:
        idx.close()

    payload = {
        "query": query,
        "mode": mode,
        "seeds": enriched,
        "file_outlines": file_outlines,
    }
    payload["token_estimate"] = _rough_token_count(payload)
    payload["hint"] = (
        "This response bundles search + callees + callers + top sources + a file outline. "
        "If it's still not enough, call `expand`, `outline`, or `source` on a specific qname."
    )
    return payload


_CONSTANT_ALIAS_RE = __import__("re").compile(r"^\s*[A-Z][A-Z0-9_]*\s*=\s*([A-Z][A-Z0-9_]*)\s*$")


def _resolve_constant_chain(
    idx, signature: str, origin_qname: str, max_hops: int = 3
) -> dict | None:
    """If ``signature`` is of the form ``NAME = OTHER_NAME``, follow the alias.

    Returns a dict describing the terminal literal value found (up to
    ``max_hops`` steps). Returns None if the RHS is already a literal or the
    chain can't be resolved.

    The search is cross-file — constants live in their own modules (commonly
    ``ai_defaults.py``-style registries). We match any qname whose tail equals
    the referenced name.
    """
    current_sig = signature
    current_qname = origin_qname
    chain: list[str] = []
    visited = {origin_qname}
    for _ in range(max_hops):
        m = _CONSTANT_ALIAS_RE.match(current_sig)
        if m is None:
            # Current sig is either a literal already (first iter) or a
            # resolved value we just reached.
            if chain:
                return {"chain": chain, "value": current_sig.split("=", 1)[1].strip(), "terminal_qname": current_qname}
            return None
        target_name = m.group(1)
        # Look up any constant whose qname ends in ":<target_name>" or
        # ".<target_name>". Exclude already-visited qnames so we don't loop
        # back to the origin or revisit an earlier hop.
        excluded = list(visited)
        placeholders = ",".join("?" * len(excluded))
        row = idx.conn.execute(
            f"SELECT qname, signature FROM symbols "
            f"WHERE kind='constant' "
            f"AND (qname = ? OR qname LIKE ? OR qname LIKE ?) "
            f"AND qname NOT IN ({placeholders}) "
            f"LIMIT 1",
            (target_name, f"%:{target_name}", f"%.{target_name}", *excluded),
        ).fetchone()
        if row is None:
            return None
        chain.append(row["qname"])
        visited.add(row["qname"])
        current_sig = row["signature"]
        current_qname = row["qname"]
    # Reached hop limit — report what we have if we resolved at least once.
    if chain:
        final_val = current_sig.split("=", 1)[1].strip() if "=" in current_sig else current_sig
        return {"chain": chain, "value": final_val, "terminal_qname": current_qname}
    return None


def _neighbor_entry(row, call_line: int) -> dict:
    return {
        "qname": row["qname"],
        "kind": row["kind"],
        "signature": row["signature"],
        "docstring": _docstring_summary(row["docstring"]),
        "line": call_line,
    }


# Unresolved callees that boil down to a stdlib builtin or method-dispatch
# primitive are almost never useful in a call graph. Drop them from context()
# output so the agent focuses on domain code.
_PY_BUILTIN_NOISE = frozenset({
    "print", "len", "range", "enumerate", "zip", "map", "filter",
    "sorted", "reversed", "any", "all", "min", "max", "sum", "abs",
    "str", "int", "float", "bool", "list", "dict", "tuple", "set",
    "bytes", "bytearray", "frozenset", "open", "type", "id", "repr",
    "hash", "ord", "chr", "next", "iter", "callable",
    "isinstance", "issubclass", "hasattr", "getattr", "setattr", "delattr",
    "format", "vars", "dir", "super", "object", "property", "staticmethod",
    "classmethod",
})

# Common JS/TS method-dispatch names used as ``X.method()`` on arrays, strings,
# Maps, Sets, Promises, etc. These show up as unresolved callees like
# ``arr.forEach`` / ``map.set`` / ``channels.push`` and crowd out real edges.
# We drop them regardless of what ``X`` is.
_JS_METHOD_NOISE = frozenset({
    # Array
    "forEach", "map", "filter", "reduce", "reduceRight", "push", "pop",
    "shift", "unshift", "slice", "splice", "concat", "join", "find",
    "findIndex", "findLast", "findLastIndex", "flat", "flatMap", "some",
    "every", "includes", "indexOf", "lastIndexOf", "reverse", "sort",
    "copyWithin", "fill", "at",
    # String (overlaps with Array.includes/indexOf, fine)
    "split", "trim", "trimStart", "trimEnd", "toLowerCase", "toUpperCase",
    "replace", "replaceAll", "substring", "substr", "charAt", "charCodeAt",
    "padStart", "padEnd", "startsWith", "endsWith", "repeat", "codePointAt",
    "normalize", "localeCompare", "match", "matchAll", "search",
    # Map / Set
    "has", "get", "set", "delete", "clear", "keys", "values", "entries",
    "add",
    # Promise / thenable
    "then", "catch", "finally",
    # JSON
    "parse", "stringify",
    # Object (when seen as Object.X or x.X that aliases the same)
    "assign", "fromEntries", "freeze", "isFrozen",
    # Common globals, as `?:clearTimeout` (no dot)
    "clearTimeout", "setTimeout", "clearInterval", "setInterval",
    "queueMicrotask", "requestAnimationFrame", "cancelAnimationFrame",
    "structuredClone",
})


def _is_builtin_noise(unresolved_qname: str) -> bool:
    """True if this unresolved callee is Python/JS stdlib noise worth dropping.

    Matches three patterns:
      * ``?:foo`` where ``foo`` is a bare Python builtin (``len``, ``print``).
      * ``?:foo`` where ``foo`` is a bare JS global (``clearTimeout``).
      * ``?:X.method`` where ``method`` is a common JS method-dispatch name
        on arrays, strings, Maps, Sets, Promises, etc. — ``X`` is usually a
        local variable or parameter we can't resolve, and ``method``
        identifies the call as stdlib dispatch regardless.
    """
    if not unresolved_qname.startswith("?:"):
        return False
    name = unresolved_qname[2:]
    if "." not in name:
        return name in _PY_BUILTIN_NOISE or name in _JS_METHOD_NOISE
    # Dotted: drop if tail is a JS method-dispatch builtin.
    tail = name.rpartition(".")[2]
    return tail in _JS_METHOD_NOISE


def _collect_neighbors(
    idx: Index,
    qname: str,
    *,
    direction: Literal["callees", "callers"],
    limit: int,
    depth: int,
) -> list[dict]:
    """Gather direction-specific neighbors of ``qname`` up to ``depth`` hops.

    Each resolved entry gets a nested ``callees`` (when direction='callees')
    or ``callers`` (when direction='callers') with the next hop's neighbors.
    Unresolved entries never recurse — we don't know what they call. Depth-2
    neighbors use a tighter limit (half, minimum 3) to keep payloads bounded.
    """
    rows = idx.callees_of(qname) if direction == "callees" else idx.callers_of(qname)
    out: list[dict] = []
    for row in rows:
        if len(out) >= limit:
            break
        if direction == "callees":
            neigh_qname = row["callee_qname"] or f"?:{row['callee_name']}"
            if _is_builtin_noise(neigh_qname):
                continue
            if row["callee_qname"]:
                nrow = idx.get_symbol(neigh_qname)
                if nrow is not None:
                    entry = _neighbor_entry(nrow, row["line"])
                    if depth > 1:
                        nested = _collect_neighbors(
                            idx, neigh_qname, direction=direction,
                            limit=max(3, limit // 2), depth=depth - 1,
                        )
                        if nested:
                            entry["callees"] = nested
                    out.append(entry)
                    continue
            out.append({"qname": neigh_qname, "line": row["line"], "resolved": False})
        else:
            nrow = idx.get_symbol(row["caller_qname"])
            if nrow is None:
                continue
            entry = _neighbor_entry(nrow, row["line"])
            if depth > 1:
                nested = _collect_neighbors(
                    idx, row["caller_qname"], direction=direction,
                    limit=max(3, limit // 2), depth=depth - 1,
                )
                if nested:
                    entry["callers"] = nested
            out.append(entry)
    return out


def _parse_line_range(lines: str) -> tuple[int, int]:
    if "-" in lines:
        a, b = lines.split("-", 1)
        return int(a), int(b)
    n = int(lines)
    return n, n


def _rough_token_count(payload: dict) -> int:
    """Approximate token count as chars/4 over the payload's JSON rendering."""
    import json
    return len(json.dumps(payload)) // 4


# ---------- indexing entry point (called by CLI) ----------


def index_root(root: str | Path) -> dict:
    """Index (or re-index) every supported source file under ``root``.

    Reads ``<root>/snapctx.toml`` if present to override the walker's
    skip lists, language enable list, or size cap. Without a config
    file, behavior is identical to the pre-config version.

    Incremental: files whose SHA matches the stored value are skipped.
    Returns a summary dict with counts.
    """
    from snapctx.config import load_config
    from snapctx.index import sha_bytes
    from snapctx.parsers.registry import parser_for
    from snapctx.walker import iter_source_files

    root_path = Path(root).resolve()
    cfg = load_config(root_path)
    idx = Index(db_path_for(root_path))
    scanned = 0
    updated = 0
    skipped = 0
    symbol_count = 0
    demoted = 0
    embedded = 0
    removed = 0
    try:
        # Snapshot the current filesystem view and diff it against the DB so we
        # can clean up rows for files that have been deleted / renamed / moved
        # into .gitignore since the last index. Without this step, stale
        # symbols and call edges accumulate indefinitely.
        walker_files = {str(f.resolve()) for f in iter_source_files(root_path, cfg.walker)}
        db_files = {
            row["path"]
            for row in idx.conn.execute("SELECT path FROM files").fetchall()
        }
        for stale in db_files - walker_files:
            idx.forget_file(stale)
            removed += 1

        for file_str in walker_files:
            file = Path(file_str)
            scanned += 1
            data = file.read_bytes()
            sha = sha_bytes(data)
            if idx.current_sha(file_str) == sha:
                skipped += 1
                continue
            parser = parser_for(file.suffix)
            assert parser is not None   # walker already filtered
            result = parser.parse(file, root_path)
            idx.ingest(file_str, parser.language, sha, result)
            updated += 1
            symbol_count += len(result.symbols)
        # Post-pass 1a: demote optimistic callee_qnames that didn't land on
        # a real symbol. Must run before promote so bogus MRO guesses (e.g.
        # ``self.x`` guessed against an imported base) are nulled first.
        demoted = idx.demote_unresolved_calls()
        # Post-pass 1b: promote forward-referenced self.X() calls that now
        # resolve against the complete symbol table (methods defined later
        # in the same class body than the caller).
        idx.promote_self_calls()
        # Post-pass 2: embed any new symbols that don't yet have vectors.
        missing = idx.symbols_without_vectors()
        if missing:
            from snapctx.embeddings import embed_texts, symbol_text_for_embedding

            texts = [
                symbol_text_for_embedding(m["qname"], m["signature"], m["docstring"])
                for m in missing
            ]
            vectors = embed_texts(texts)
            idx.upsert_vectors([m["qname"] for m in missing], vectors)
            embedded = len(missing)
    finally:
        idx.close()

    return {
        "root": str(root_path),
        "files_scanned": scanned,
        "files_updated": updated,
        "files_unchanged": skipped,
        "files_removed": removed,
        "symbols_indexed": symbol_count,
        "calls_demoted": demoted,
        "symbols_embedded": embedded,
    }


# ---------- multi-root wrappers ----------
#
# When the CLI is launched from a parent directory containing several
# indexed sub-projects (a backend/ + frontend/ monorepo, say),
# ``discover_roots`` returns more than one root. The wrappers below fan
# out queries across roots, merge or route results, and tag each entry
# with the root it came from so the caller knows which sub-project a
# symbol lives in.
#
# All wrappers accept ``anchor=`` — the directory the user invoked the
# command from. It's only used to compute friendly relative ``root``
# labels in the response (e.g. ``backend`` vs ``frontend``); query
# behavior is independent of it.


def _run_in_parallel(fn, roots: list[Path]) -> list[tuple[Path, object]]:
    """Run ``fn(root)`` for each root concurrently. Return (root, result) pairs.

    SQLite + embedding workloads are largely I/O / C-bound, so a thread pool
    is the right shape here. Errors per root surface as result dicts with
    an ``error`` key — one bad root shouldn't poison the whole response.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    out: list[tuple[Path, object]] = []
    if not roots:
        return out
    if len(roots) == 1:
        try:
            return [(roots[0], fn(roots[0]))]
        except Exception as e:
            return [(roots[0], {"error": f"{type(e).__name__}: {e}"})]

    with ThreadPoolExecutor(max_workers=min(8, len(roots))) as pool:
        futures = {pool.submit(fn, r): r for r in roots}
        for fut in as_completed(futures):
            r = futures[fut]
            try:
                out.append((r, fut.result()))
            except Exception as e:
                out.append((r, {"error": f"{type(e).__name__}: {e}"}))
    # Stable order: same as ``roots``.
    order = {r: i for i, r in enumerate(roots)}
    out.sort(key=lambda pair: order.get(pair[0], 1_000_000))
    return out


def search_code_multi(
    query: str,
    roots: list[Path],
    *,
    k: int = 5,
    kind: str | None = None,
    mode: Literal["lexical", "vector", "hybrid"] = "hybrid",
    anchor: Path | None = None,
) -> dict:
    """Run ``search_code`` across multiple roots and merge by score.

    Each result is tagged with ``root`` (a short label relative to ``anchor``)
    so the caller can tell where a symbol lives. The merged list is sorted
    by score across roots; only the global top-``k`` are returned.
    """
    from snapctx.roots import root_label

    if not roots:
        return {"query": query, "mode": mode, "results": [], "hint": "No indexed roots."}

    per_root = _run_in_parallel(
        lambda r: search_code(query, k=k, kind=kind, root=r, mode=mode),
        roots,
    )

    merged: list[dict] = []
    errors: list[dict] = []
    for r, res in per_root:
        if isinstance(res, dict) and "error" in res:
            errors.append({"root": root_label(r, anchor), "error": res["error"]})
            continue
        label = root_label(r, anchor)
        for item in res.get("results", []):
            item = dict(item)
            item["root"] = label
            merged.append(item)

    merged.sort(key=lambda x: -float(x.get("score", 0.0)))
    top = merged[:k]

    payload = {
        "query": query,
        "mode": mode,
        "roots": [root_label(r, anchor) for r in roots],
        "results": top,
        "hint": _search_hint(top),
    }
    if errors:
        payload["root_errors"] = errors
    return payload


def context_multi(
    query: str,
    roots: list[Path],
    *,
    k_seeds: int = 5,
    source_for_top: int = 5,
    expand_depth: int = 2,
    neighbor_limit: int = 8,
    body_char_cap: int = 2000,
    file_outline_limit: int = 8,
    outline_discovery_k: int = 15,
    mode: Literal["lexical", "vector", "hybrid"] = "hybrid",
    kind: str | None = None,
    anchor: Path | None = None,
) -> dict:
    """Run ``context`` across multiple roots and merge into one pack.

    Each root produces its own seeds + file outlines. We combine, sort
    seeds by score, keep global top ``k_seeds``, and concatenate file
    outlines (each tagged with its root). Source bodies attached to
    seeds in their per-root pass are preserved through the merge.
    """
    from snapctx.roots import root_label

    if not roots:
        return {
            "query": query,
            "mode": mode,
            "seeds": [],
            "hint": "No indexed roots.",
            "token_estimate": 0,
        }

    per_root = _run_in_parallel(
        lambda r: context(
            query,
            k_seeds=k_seeds,
            source_for_top=source_for_top,
            expand_depth=expand_depth,
            neighbor_limit=neighbor_limit,
            body_char_cap=body_char_cap,
            file_outline_limit=file_outline_limit,
            outline_discovery_k=outline_discovery_k,
            mode=mode,
            kind=kind,
            root=r,
        ),
        roots,
    )

    all_seeds: list[dict] = []
    all_outlines: list[dict] = []
    errors: list[dict] = []
    for r, res in per_root:
        if isinstance(res, dict) and "error" in res:
            errors.append({"root": root_label(r, anchor), "error": res["error"]})
            continue
        label = root_label(r, anchor)
        for s in res.get("seeds", []):
            s = dict(s)
            s["root"] = label
            all_seeds.append(s)
        for fo in res.get("file_outlines", []):
            fo = dict(fo)
            fo["root"] = label
            all_outlines.append(fo)

    # Sort seeds by score (RRF scores are comparable across roots — same
    # ranker, same fusion). Take global top-K.
    all_seeds.sort(key=lambda s: -float(s.get("score", 0.0)))
    top_seeds = all_seeds[:k_seeds]
    # Renumber rank to reflect global position.
    for i, s in enumerate(top_seeds, start=1):
        s["rank"] = i

    # Cap total file outlines too — same budget across roots.
    capped_outlines = all_outlines[: file_outline_limit]

    payload = {
        "query": query,
        "mode": mode,
        "roots": [root_label(r, anchor) for r in roots],
        "seeds": top_seeds,
        "file_outlines": capped_outlines,
    }
    payload["token_estimate"] = _rough_token_count(payload)
    payload["hint"] = (
        "Multi-root context: results merged across "
        f"{len(roots)} indexed sub-project(s). Each seed has a `root` field "
        "showing which one it came from."
    )
    if errors:
        payload["root_errors"] = errors
    return payload


def expand_multi(
    qname: str,
    roots: list[Path],
    *,
    direction: Literal["callees", "callers", "both"] = "callees",
    depth: int = 1,
    anchor: Path | None = None,
) -> dict:
    """Route ``expand`` to whichever root contains ``qname``.

    qnames are namespaced by module path, so collisions across sub-projects
    are unusual; if they happen, the first root in ``roots`` order wins.
    """
    from snapctx.roots import root_label, route_by_qname

    target = route_by_qname(qname, roots)
    if target is None:
        return {
            "qname": qname,
            "error": "not_found",
            "hint": (
                f"No symbol named {qname!r} in any of the "
                f"{len(roots)} indexed root(s). Call search_code first to find valid qnames."
            ),
            "roots_tried": [root_label(r, anchor) for r in roots],
        }
    result = expand(qname, direction=direction, depth=depth, root=target)
    result["root"] = root_label(target, anchor)
    return result


def get_source_multi(
    qname: str,
    roots: list[Path],
    *,
    with_neighbors: bool = False,
    anchor: Path | None = None,
) -> dict:
    """Route ``get_source`` to whichever root contains ``qname``."""
    from snapctx.roots import root_label, route_by_qname

    target = route_by_qname(qname, roots)
    if target is None:
        return {
            "qname": qname,
            "error": "not_found",
            "hint": f"No symbol {qname!r} in any indexed root.",
            "roots_tried": [root_label(r, anchor) for r in roots],
        }
    result = get_source(qname, with_neighbors=with_neighbors, root=target)
    result["root"] = root_label(target, anchor)
    return result


def outline_multi(
    path: str | Path,
    roots: list[Path],
    *,
    anchor: Path | None = None,
) -> dict:
    """Route ``outline`` to the root whose dir is the longest prefix of ``path``.

    For relative paths, ``anchor`` is used as the resolution base; if no
    root contains the resolved file, fall back to trying each root in
    order (the first non-empty outline wins).
    """
    from snapctx.roots import root_label, route_by_path

    p = Path(path)
    if not p.is_absolute() and anchor is not None:
        p = (anchor / p).resolve()
    elif p.is_absolute():
        p = p.resolve()

    target = route_by_path(p, roots)
    if target is not None:
        result = outline(p, root=target)
        result["root"] = root_label(target, anchor)
        return result

    # Fall back: try each root, return the first that has matches.
    for r in roots:
        result = outline(path, root=r)
        if result.get("symbols"):
            result["root"] = root_label(r, anchor)
            return result

    return {
        "file": str(p),
        "symbols": [],
        "hint": (
            f"No symbols indexed for {p} in any of the "
            f"{len(roots)} root(s). Did you index the right project?"
        ),
        "roots_tried": [root_label(r, anchor) for r in roots],
    }
