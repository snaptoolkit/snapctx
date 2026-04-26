"""``context`` — one-shot retrieval that bundles search + graph walk + sources.

This is the agent's "first move" tool: one call returns enough structured
context to answer most code-understanding questions without follow-up.
The orchestration is procedural by design (the steps are sequential and
have clear before/after relationships) and small enough to read top-to-bottom.

Pipeline:

1. **Fast path**: if the query is an exact qname (``mod:Symbol``), skip
   search and seed directly.
2. **Search**: overfetch to ``outline_discovery_k`` for file-outline
   coverage, take top ``k_seeds`` as the agent-visible seeds.
3. **Enrich**: per seed, attach callees + callers (depth=2), full source
   for the top ``source_for_top``, and constant-alias resolution for
   constant seeds.
4. **File outlines**: walk the broader candidate pool (not just the
   visible seeds) so survey questions ("list every route") get coverage
   even when seeds cluster.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from snapctx.api._aliases import resolve_constant_chain
from snapctx.api._common import (
    docstring_summary,
    open_index,
    parse_line_range,
    rough_token_count,
)
from snapctx.api._graph import collect_neighbors
from snapctx.api._search import search_code


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
    scope: str | None = None,
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
    ``file_outline_limit`` unique files to outline.
    """
    root_path = Path(root).resolve()
    seeds, candidates, mode = _seeds_for_query(
        query, root_path, k_seeds, outline_discovery_k, kind, mode, scope=scope,
    )

    if not seeds:
        return {
            "query": query,
            "mode": mode,
            "seeds": [],
            "hint": "No matches. Try different keywords, or widen with a conceptual query (e.g. 'retry logic' vs 'ErrorHandler').",
            "token_estimate": 0,
        }

    idx = open_index(root_path, scope=scope)
    try:
        enriched = [
            _enrich_seed(
                idx, seed, rank=i + 1,
                neighbor_limit=neighbor_limit,
                expand_depth=expand_depth,
                source_for_top=source_for_top,
                body_char_cap=body_char_cap,
            )
            for i, seed in enumerate(seeds)
        ]
        file_outlines = _file_outlines(idx, candidates, file_outline_limit)
    finally:
        idx.close()

    payload: dict = {
        "query": query,
        "mode": mode,
        "seeds": enriched,
        "file_outlines": file_outlines,
    }
    if scope is not None:
        payload["scope"] = scope
    payload["token_estimate"] = rough_token_count(payload)
    payload["hint"] = (
        "This response bundles search + callees + callers + top sources + a file outline. "
        "If it's still not enough, call `expand`, `outline`, or `source` on a specific qname."
    )
    return payload


def _seeds_for_query(
    query: str,
    root_path: Path,
    k_seeds: int,
    outline_discovery_k: int,
    kind: str | None,
    mode: str,
    *,
    scope: str | None = None,
) -> tuple[list[dict], list[dict], str]:
    """Return ``(visible_seeds, broader_candidates, effective_mode)``.

    Encapsulates the exact-qname fast path so the main orchestrator stays
    linear. ``effective_mode`` is "exact" when the fast path fired, else
    the original mode.
    """
    if ":" in query:
        idx_tmp = open_index(root_path, scope=scope)
        try:
            direct = idx_tmp.get_symbol(query)
        finally:
            idx_tmp.close()
        if direct is not None:
            seed = {
                "qname": direct["qname"],
                "kind": direct["kind"],
                "signature": direct["signature"],
                "docstring": docstring_summary(direct["docstring"]),
                "file": direct["file"],
                "lines": f"{direct['line_start']}-{direct['line_end']}",
                "score": 1.0,
                "decorators": direct["decorators"].split("\n") if direct["decorators"] else None,
            }
            return [seed], [seed], "exact"

    search_result = search_code(
        query, k=max(k_seeds, outline_discovery_k), kind=kind,
        root=root_path, mode=mode, scope=scope,
    )
    candidates = search_result["results"]
    return candidates[:k_seeds], candidates, mode


def _enrich_seed(
    idx,
    seed: dict,
    *,
    rank: int,
    neighbor_limit: int,
    expand_depth: int,
    source_for_top: int,
    body_char_cap: int,
) -> dict:
    """Attach callees/callers, source, and (for constants) alias resolution."""
    qname = seed["qname"]
    entry = {
        "rank": rank,
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

    depth = max(1, expand_depth)
    callees = collect_neighbors(idx, qname, direction="callees", limit=neighbor_limit, depth=depth)
    callers = collect_neighbors(idx, qname, direction="callers", limit=neighbor_limit, depth=depth)
    if callees:
        entry["callees"] = callees
    if callers:
        entry["callers"] = callers

    if rank <= source_for_top:
        try:
            src = Path(seed["file"]).read_text(encoding="utf-8", errors="replace")
            lines = src.splitlines()
            start, end = parse_line_range(seed.get("lines", "1-1"))
            body = "\n".join(lines[start - 1 : end])
            if len(body) > body_char_cap:
                body = body[:body_char_cap] + f"\n# ... truncated ({len(body) - body_char_cap} chars) ..."
            entry["source"] = body
        except OSError as e:
            entry["source_error"] = str(e)

    if seed["kind"] == "constant":
        resolved = resolve_constant_chain(idx, seed["signature"], qname)
        if resolved is not None:
            entry["resolved_value"] = resolved

    return entry


def _file_outlines(idx, candidates: list[dict], limit: int) -> list[dict]:
    """Outline up to ``limit`` unique files from the broader candidate pool.

    This is what closes the "survey" gap: when the top-K seeds cluster in
    a couple of files but the user asked something like "list every route",
    we use the wider candidate pool to discover more files to outline.
    """
    seen_files: list[str] = []
    for cand in candidates:
        if cand["file"] not in seen_files:
            seen_files.append(cand["file"])
        if len(seen_files) >= limit:
            break
    out: list[dict] = []
    for f in seen_files:
        rows = idx.symbols_in_file(f)
        if not rows:
            continue
        out.append({
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
        })
    return out
