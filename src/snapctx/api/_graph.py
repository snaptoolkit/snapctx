"""Call-graph operations: ``expand`` and the depth-walking helpers ``context`` uses.

Two public surfaces:

* ``expand(qname, direction, depth)`` — layered BFS over the call graph,
  returning neighbor signatures (no bodies) per hop.
* ``collect_neighbors(idx, qname, direction, limit, depth)`` — internal
  helper used by ``context`` to inline the depth-2 callee/caller trace
  under each seed.

The builtin-noise filter (``is_builtin_noise``) drops Python/JS stdlib
method dispatch (``?:print``, ``?:arr.forEach``) so the call graph
focuses on domain code.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Literal

from snapctx.api._common import (
    docstring_summary,
    open_index,
    row_to_symbol_dict,
)
from snapctx.api._cross_package import CrossPackageResolver
from snapctx.index import Index


# ---------- builtin-noise filter ----------

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


def is_builtin_noise(unresolved_qname: str) -> bool:
    """True if this unresolved callee is Python/JS stdlib noise worth dropping."""
    if not unresolved_qname.startswith("?:"):
        return False
    name = unresolved_qname[2:]
    if "." not in name:
        return name in _PY_BUILTIN_NOISE or name in _JS_METHOD_NOISE
    tail = name.rpartition(".")[2]
    return tail in _JS_METHOD_NOISE


# ---------- expand ----------


def expand(
    qname: str,
    direction: Literal["callees", "callers", "both"] = "callees",
    depth: int = 1,
    root: str | Path = ".",
    scope: str | None = None,
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
    root_path = Path(root).resolve()
    idx = open_index(root_path, scope=scope)
    resolver = CrossPackageResolver(root_path, current_scope=scope)
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

        for _ in range(1, depth + 1):
            next_frontier: list[str] = []
            layer: list[dict] = []
            for source_qname in frontier:
                for neigh_qname, neigh_row, edge_kind, call_line, pkg in _neighbors(
                    idx, source_qname, direction, resolver,
                ):
                    if neigh_qname in visited:
                        continue
                    visited.add(neigh_qname)
                    # Cross-package neighbors live in another index, so
                    # we can't traverse their callees/callers from the
                    # current frontier — keep them as terminal entries.
                    if pkg is None:
                        next_frontier.append(neigh_qname)
                    entry = {"from": source_qname, "edge": edge_kind, "line": call_line}
                    if neigh_row is not None:
                        entry.update(row_to_symbol_dict(neigh_row))
                        entry["docstring"] = docstring_summary(neigh_row["docstring"])
                        if pkg is not None:
                            entry["package"] = pkg
                    else:
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
        resolver.close()
        idx.close()


def _neighbors(
    idx: Index, qname: str, direction: str, resolver: CrossPackageResolver,
) -> list[tuple[str, sqlite3.Row | None, str, int, str | None]]:
    """Return (neighbor_qname, neighbor_symbol_row_or_None, edge_kind, line, package_or_None) tuples.

    ``package`` is non-None when the neighbor was resolved by following an
    import into a sibling vendor package's index; in that case the row
    comes from that other index.
    """
    out: list[tuple[str, sqlite3.Row | None, str, int, str | None]] = []
    if direction in ("callees", "both"):
        for row in idx.callees_of(qname):
            if row["callee_qname"]:
                sym = idx.get_symbol(row["callee_qname"])
                out.append((row["callee_qname"], sym, "callee", row["line"], None))
                continue
            cross = resolver.resolve(
                row["callee_name"], row["file"], idx,
            )
            if cross is not None:
                pkg = cross["package"]
                xrow = cross["row"]
                tagged_qname = f"{pkg}::{xrow['qname']}"
                out.append((tagged_qname, xrow, "callee", row["line"], pkg))
                continue
            out.append(
                (f"?:{row['callee_name']}", None, "callee", row["line"], None)
            )
    if direction in ("callers", "both"):
        for row in idx.callers_of(qname):
            neigh_qname = row["caller_qname"]
            sym = idx.get_symbol(neigh_qname)
            out.append((neigh_qname, sym, "caller", row["line"], None))
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


# ---------- depth walking for context() ----------


def neighbor_entry(row, call_line: int) -> dict:
    return {
        "qname": row["qname"],
        "kind": row["kind"],
        "signature": row["signature"],
        "docstring": docstring_summary(row["docstring"]),
        "line": call_line,
    }


def collect_neighbors(
    idx: Index,
    qname: str,
    *,
    direction: Literal["callees", "callers"],
    limit: int,
    depth: int,
    resolver: CrossPackageResolver | None = None,
) -> list[dict]:
    """Gather direction-specific neighbors of ``qname`` up to ``depth`` hops.

    Each resolved entry gets a nested ``callees`` (when direction='callees')
    or ``callers`` (when direction='callers') with the next hop's neighbors.
    Unresolved entries never recurse — we don't know what they call. Depth-2
    neighbors use a tighter limit (half, minimum 3) to keep payloads bounded.

    ``resolver`` enables cross-package fallback: when a callee is unresolved
    in this index, attempt to resolve it via the file's imports against
    sibling vendor indexes that have already been built. Cross-package
    hits are tagged with a ``package`` field and don't recurse (their own
    callees live in the other index).
    """
    rows = idx.callees_of(qname) if direction == "callees" else idx.callers_of(qname)
    out: list[dict] = []
    for row in rows:
        if len(out) >= limit:
            break
        if direction == "callees":
            neigh_qname = row["callee_qname"] or f"?:{row['callee_name']}"
            if is_builtin_noise(neigh_qname):
                continue
            if row["callee_qname"]:
                nrow = idx.get_symbol(neigh_qname)
                if nrow is not None:
                    entry = neighbor_entry(nrow, row["line"])
                    if depth > 1:
                        nested = collect_neighbors(
                            idx, neigh_qname, direction=direction,
                            limit=max(3, limit // 2), depth=depth - 1,
                            resolver=resolver,
                        )
                        if nested:
                            entry["callees"] = nested
                    out.append(entry)
                    continue
            if resolver is not None:
                cross = resolver.resolve(row["callee_name"], row["file"], idx)
                if cross is not None:
                    entry = neighbor_entry(cross["row"], row["line"])
                    entry["package"] = cross["package"]
                    out.append(entry)
                    continue
            out.append({"qname": neigh_qname, "line": row["line"], "resolved": False})
        else:
            nrow = idx.get_symbol(row["caller_qname"])
            if nrow is None:
                continue
            entry = neighbor_entry(nrow, row["line"])
            if depth > 1:
                nested = collect_neighbors(
                    idx, row["caller_qname"], direction=direction,
                    limit=max(3, limit // 2), depth=depth - 1,
                    resolver=resolver,
                )
                if nested:
                    entry["callers"] = nested
            out.append(entry)
    return out
