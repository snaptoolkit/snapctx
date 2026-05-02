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
from snapctx.api._find import find_literal
from snapctx.api._graph import collect_neighbors
from snapctx.api._ranking import extract_audit_literal
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

    audit_block = _maybe_audit_find(query, root_path, scope)

    if not seeds:
        payload: dict = {
            "query": query,
            "mode": mode,
            "seeds": [],
        }
        if audit_block is not None:
            payload["find_results"] = audit_block
        payload["token_estimate"] = 0 if audit_block is None else rough_token_count(payload)
        payload["hint"] = (
            _context_hint(audit_block) if audit_block is not None
            else "No matches. Try different keywords, or widen with a conceptual query (e.g. 'retry logic' vs 'ErrorHandler')."
        )
        return payload

    from snapctx.api._cross_package import CrossPackageResolver
    idx = open_index(root_path, scope=scope)
    resolver = CrossPackageResolver(root_path, current_scope=scope)
    try:
        enriched = [
            _enrich_seed(
                idx, seed, rank=i + 1,
                neighbor_limit=neighbor_limit,
                expand_depth=expand_depth,
                source_for_top=source_for_top,
                body_char_cap=body_char_cap,
                resolver=resolver,
            )
            for i, seed in enumerate(seeds)
        ]
        file_outlines = _file_outlines(idx, candidates, file_outline_limit)
    finally:
        resolver.close()
        idx.close()

    payload = {
        "query": query,
        "mode": mode,
        "seeds": enriched,
        "file_outlines": file_outlines,
    }
    if scope is not None:
        payload["scope"] = scope

    if audit_block is not None:
        payload["find_results"] = audit_block

    payload["token_estimate"] = rough_token_count(payload)
    _apply_payload_guard(payload, body_char_cap)
    payload["hint"] = _context_hint(audit_block, payload.get("trimmed"))
    return payload


# Threshold above which ``context`` aggressively trims its response.
# 8 k tokens is roughly 32 KB of text — past this, the value of more
# context drops sharply (the agent has plenty of seeds to act on) and
# the cost of more tokens is real. Hard budget at ~2× soft so a single
# huge seed body can still survive when it actually carries the answer.
_SOFT_TOKEN_BUDGET = 8000
_HARD_TOKEN_BUDGET = 16000


def _apply_payload_guard(payload: dict, body_char_cap: int) -> None:
    """Trim broad-query payloads in place, marking the response as trimmed.

    Two stages, applied only when needed:

    1. **Soft overflow** (> ``_SOFT_TOKEN_BUDGET``): drop ``file_outlines``.
       The seeds carry the load-bearing code; outlines are extra
       structure that on broad framework/routing queries can balloon to
       half the response.
    2. **Hard overflow** (still > ``_HARD_TOKEN_BUDGET`` after stage 1):
       halve the per-seed body cap so each seed shrinks. Better to lose
       some bytes from each seed than to drop a seed entirely.

    Sets ``payload["trimmed"] = "soft"`` or ``"hard"`` so the hint can
    nudge the agent toward a scoped follow-up — broad ``context`` on
    framework/routing questions overmatches by design, and the user
    almost always has a directional hint that ``snapctx_grep --in-path``
    or ``snapctx_search`` would resolve faster.
    """
    if payload.get("token_estimate", 0) <= _SOFT_TOKEN_BUDGET:
        return
    payload["trimmed"] = "soft"
    if payload.get("file_outlines"):
        payload["file_outlines"] = []
        payload["token_estimate"] = rough_token_count(payload)
    if payload["token_estimate"] <= _HARD_TOKEN_BUDGET:
        return
    payload["trimmed"] = "hard"
    half_cap = max(400, body_char_cap // 2)
    for seed in payload.get("seeds", []):
        body = seed.get("source")
        if isinstance(body, str) and len(body) > half_cap:
            seed["source"] = (
                body[:half_cap]
                + f"\n# ... truncated ({len(body) - half_cap} chars) ..."
            )
    payload["token_estimate"] = rough_token_count(payload)


def _maybe_audit_find(
    query: str, root_path: Path, scope: str | None,
) -> dict | None:
    """Run ``find`` alongside ``context`` when the query is an unambiguous audit.

    Triggered only when ``extract_audit_literal`` returns a single concrete
    literal — multi-literal questions ("every LLM provider") are deliberately
    skipped because the agent will get a better answer by enumerating the
    literals from the ranked seeds and calling ``find --also``. We surface
    the literal we picked so the agent can sanity-check the choice.
    """
    literal = extract_audit_literal(query)
    if literal is None:
        return None
    found = find_literal(
        literal, root=root_path, scope=scope,
        with_bodies=False, max_results=200,
    )
    if not found["matches"]:
        return None
    # Project to the minimum the agent needs: file:line + qname + the matching
    # source line. With this they can answer most "every X" questions without
    # a follow-up call; for full bodies they re-issue ``find <lit> --with-bodies``.
    return {
        "literal": literal,
        "match_count": found["match_count"],
        "truncated": found["truncated"],
        "matches": [
            {
                "qname": m["qname"],
                "file": m["file"],
                "match_line": m["match_line"],
                "match_text": m["match_text"],
            }
            for m in found["matches"]
        ],
    }



def _context_hint(audit_block: dict | None, trimmed: str | None = None) -> str:
    base = (
        "This response bundles search + callees + callers + top sources + a file outline. "
        "If it's still not enough, call `expand`, `outline`, or `source` on a specific qname."
    )
    trim_note = ""
    if trimmed == "soft":
        trim_note = (
            "Broad query — payload was over the soft budget, so file outlines "
            "were dropped. For a tighter follow-up: `snapctx_grep \"<literal>\" "
            "--in-path <subdir>` for wiring, `snapctx_search \"<name>\"` for a "
            "known symbol, or `snapctx_outline <file>` for one file's full "
            "structure. "
        )
    elif trimmed == "hard":
        trim_note = (
            "Broad query — payload was over the hard budget, so seed bodies "
            "were further truncated. Strongly consider scoping the next call: "
            "`snapctx_grep \"<literal>\" --in-path <subdir>`, "
            "`snapctx_search \"<name>\"`, or `snapctx_source <qname>` on the "
            "exact symbol you want full source for. "
        )
    if audit_block is None:
        return trim_note + base
    lit = audit_block["literal"]
    n = audit_block["match_count"]
    return (
        f"Audit-class query detected: ran find({lit!r}) for exhaustive coverage — "
        f"{n} sites in `find_results`. For full bodies of every site, call "
        f"`find {lit!r} --with-bodies`. "
    ) + trim_note + base


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
    resolver=None,
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
    callees = collect_neighbors(
        idx, qname, direction="callees",
        limit=neighbor_limit, depth=depth, resolver=resolver,
    )
    callers = collect_neighbors(
        idx, qname, direction="callers",
        limit=neighbor_limit, depth=depth, resolver=resolver,
    )
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
