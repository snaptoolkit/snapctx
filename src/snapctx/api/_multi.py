"""Multi-root fan-out for queries against several indexed sub-projects.

When ``discover_roots`` returns more than one root (typical monorepo
parent with ``backend/`` + ``frontend/`` each indexed separately), these
wrappers run the per-root operation in parallel and merge or route
results so the agent sees one coherent payload.

Two patterns:

* **Merge** — for ``search`` / ``context``. Run on every root, tag
  each result with its root label, sort by score, take global top-K.
* **Route** — for ``expand`` / ``source`` / ``outline``. Pick the root
  most likely to own the symbol or path, delegate to it, tag the
  result with the chosen root.

Both patterns share the parallel-execute + per-root error capture,
which lives in ``_fan_out``. Each wrapper then provides only its
specific merge or routing strategy — no boilerplate.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Literal

from snapctx.api._context import context
from snapctx.api._graph import expand
from snapctx.api._ranking import search_hint
from snapctx.api._retrieve import get_source, outline
from snapctx.api._search import search_code


# ---------- shared fan-out plumbing ----------


def _fan_out(
    operation: Callable[[Path], dict],
    roots: list[Path],
    *,
    anchor: Path | None = None,
) -> tuple[list[tuple[Path, dict]], list[dict]]:
    """Run ``operation(root)`` on each root in parallel.

    Returns ``(successful_results, errors)``. Each successful entry is a
    ``(root, result_dict)`` pair; each error is ``{"root": str, "error": str}``
    where the ``root`` label is anchor-relative if ``anchor`` is given.
    Stable order — same as the input ``roots`` — so the response is
    deterministic regardless of which thread finishes first.

    SQLite + ONNX work is largely I/O / C-bound, so a thread pool is
    the right shape here. One bad root surfaces as an entry in
    ``errors`` rather than poisoning the whole response.
    """
    if not roots:
        return [], []

    raw: list[tuple[Path, Any]] = []
    if len(roots) == 1:
        try:
            raw.append((roots[0], operation(roots[0])))
        except Exception as e:
            raw.append((roots[0], {"error": f"{type(e).__name__}: {e}"}))
    else:
        with ThreadPoolExecutor(max_workers=min(8, len(roots))) as pool:
            futures = {pool.submit(operation, r): r for r in roots}
            for fut in as_completed(futures):
                r = futures[fut]
                try:
                    raw.append((r, fut.result()))
                except Exception as e:
                    raw.append((r, {"error": f"{type(e).__name__}: {e}"}))
        order = {r: i for i, r in enumerate(roots)}
        raw.sort(key=lambda pair: order.get(pair[0], 1_000_000))

    from snapctx.roots import root_label  # local: avoid circular import

    results: list[tuple[Path, dict]] = []
    errors: list[dict] = []
    for r, res in raw:
        if isinstance(res, dict) and "error" in res:
            errors.append({"root": root_label(r, anchor), "error": res["error"]})
        else:
            results.append((r, res))
    return results, errors


def _tag_items(items: list[dict], root_label_str: str) -> list[dict]:
    """Return new items with ``root`` field added — never mutates the originals."""
    return [{**it, "root": root_label_str} for it in items]


def _root_labels(roots: list[Path], anchor: Path | None) -> list[str]:
    from snapctx.roots import root_label
    return [root_label(r, anchor) for r in roots]


# ---------- merge wrappers (search / context) ----------


def search_code_multi(
    query: str,
    roots: list[Path],
    *,
    k: int = 5,
    kind: str | None = None,
    mode: Literal["lexical", "vector", "hybrid"] = "hybrid",
    with_bodies: bool = False,
    also: tuple[str, ...] | list[str] | None = None,
    anchor: Path | None = None,
) -> dict:
    """Run ``search_code`` across multiple roots and merge by score."""
    from snapctx.roots import root_label

    if not roots:
        return {"query": query, "mode": mode, "results": [], "hint": "No indexed roots."}

    ok, errors = _fan_out(
        lambda r: search_code(
            query, k=k, kind=kind, root=r, mode=mode,
            with_bodies=with_bodies, also=also,
        ),
        roots, anchor=anchor,
    )

    merged = [
        item
        for r, res in ok
        for item in _tag_items(res.get("results", []), root_label(r, anchor))
    ]
    merged.sort(key=lambda x: -float(x.get("score", 0.0)))
    top = merged[:k]

    payload: dict = {
        "query": query,
        "mode": mode,
        "roots": _root_labels(roots, anchor),
        "results": top,
        "hint": search_hint(
            top, query=query, with_bodies=with_bodies, also_used=bool(also),
        ),
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
    """Run ``context`` across multiple roots and merge into one pack."""
    from snapctx.api._common import rough_token_count
    from snapctx.roots import root_label

    if not roots:
        return {
            "query": query, "mode": mode, "seeds": [],
            "hint": "No indexed roots.", "token_estimate": 0,
        }

    ok, errors = _fan_out(
        lambda r: context(
            query, k_seeds=k_seeds, source_for_top=source_for_top,
            expand_depth=expand_depth, neighbor_limit=neighbor_limit,
            body_char_cap=body_char_cap, file_outline_limit=file_outline_limit,
            outline_discovery_k=outline_discovery_k, mode=mode, kind=kind, root=r,
        ),
        roots, anchor=anchor,
    )

    seeds: list[dict] = []
    outlines: list[dict] = []
    for r, res in ok:
        label = root_label(r, anchor)
        seeds.extend(_tag_items(res.get("seeds", []), label))
        outlines.extend(_tag_items(res.get("file_outlines", []), label))

    # RRF scores are comparable across roots — same ranker, same fusion.
    seeds.sort(key=lambda s: -float(s.get("score", 0.0)))
    top_seeds = seeds[:k_seeds]
    for i, s in enumerate(top_seeds, start=1):
        s["rank"] = i

    payload: dict = {
        "query": query,
        "mode": mode,
        "roots": _root_labels(roots, anchor),
        "seeds": top_seeds,
        "file_outlines": outlines[:file_outline_limit],
    }
    payload["token_estimate"] = rough_token_count(payload)
    payload["hint"] = (
        f"Multi-root context: results merged across {len(roots)} indexed sub-project(s). "
        "Each seed has a `root` field showing which one it came from."
    )
    if errors:
        payload["root_errors"] = errors
    return payload


# ---------- route wrappers (expand / source / outline) ----------


def _route_qname(
    qname: str,
    roots: list[Path],
    operation: Callable[[Path], dict],
    *,
    not_found_hint: str,
    anchor: Path | None,
) -> dict:
    """Find the root that owns ``qname`` and delegate to it.

    Returns the operation's result tagged with ``root``, or a clean
    ``not_found`` dict listing the roots tried.
    """
    from snapctx.roots import root_label, route_by_qname

    target = route_by_qname(qname, roots)
    if target is None:
        return {
            "qname": qname,
            "error": "not_found",
            "hint": not_found_hint,
            "roots_tried": _root_labels(roots, anchor),
        }
    result = operation(target)
    result["root"] = root_label(target, anchor)
    return result


def expand_multi(
    qname: str,
    roots: list[Path],
    *,
    direction: Literal["callees", "callers", "both"] = "callees",
    depth: int = 1,
    anchor: Path | None = None,
) -> dict:
    """Route ``expand`` to whichever root contains ``qname``."""
    return _route_qname(
        qname, roots,
        lambda r: expand(qname, direction=direction, depth=depth, root=r),
        not_found_hint=(
            f"No symbol named {qname!r} in any of the {len(roots)} indexed root(s). "
            "Call search_code first to find valid qnames."
        ),
        anchor=anchor,
    )


def get_source_multi(
    qname: str,
    roots: list[Path],
    *,
    with_neighbors: bool = False,
    anchor: Path | None = None,
) -> dict:
    """Route ``get_source`` to whichever root contains ``qname``."""
    return _route_qname(
        qname, roots,
        lambda r: get_source(qname, with_neighbors=with_neighbors, root=r),
        not_found_hint=f"No symbol {qname!r} in any indexed root.",
        anchor=anchor,
    )


def outline_multi(
    path: str | Path,
    roots: list[Path],
    *,
    max_files: int = 50,
    with_bodies: bool = False,
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
        result = outline(
            p, root=target, max_files=max_files, with_bodies=with_bodies,
        )
        result["root"] = root_label(target, anchor)
        return result

    # Fall back: try each root, return the first that has matches.
    for r in roots:
        result = outline(
            path, root=r, max_files=max_files, with_bodies=with_bodies,
        )
        if result.get("symbols") or result.get("files"):
            result["root"] = root_label(r, anchor)
            return result

    return {
        "file": str(p),
        "symbols": [],
        "hint": (
            f"No symbols indexed for {p} in any of the {len(roots)} root(s). "
            "Did you index the right project?"
        ),
        "roots_tried": _root_labels(roots, anchor),
    }
