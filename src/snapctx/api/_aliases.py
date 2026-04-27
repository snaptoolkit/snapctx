"""Cross-file constant-alias resolution.

When a constant is just an alias for another (``DEFAULT_MODEL = TRANSLATION_MODEL``),
``context()`` follows the chain so the agent sees the terminal literal value
without an extra round-trip. Lives in its own module because the chain-following
logic is meaningful enough to read in isolation.
"""

from __future__ import annotations

import re

_CONSTANT_ALIAS_RE = re.compile(r"^\s*[A-Z][A-Z0-9_]*\s*=\s*([A-Z][A-Z0-9_]*)\s*$")

# Identifier references inside a body that look like SCREAMING_SNAKE constants.
# We use this for the audit-class enrichment in ``search --with-bodies``: any
# such reference is a likely candidate for cross-file constant lookup so the
# agent doesn't have to chase ``DEFAULT_*_MODEL`` to find its literal value.
_UPPER_REF_RE = re.compile(r"\b([A-Z][A-Z0-9_]{2,})\b")


def resolve_constant_chain(
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
            if chain:
                return {
                    "chain": chain,
                    "value": current_sig.split("=", 1)[1].strip(),
                    "terminal_qname": current_qname,
                }
            return None
        target_name = m.group(1)
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
    if chain:
        final_val = current_sig.split("=", 1)[1].strip() if "=" in current_sig else current_sig
        return {"chain": chain, "value": final_val, "terminal_qname": current_qname}
    return None


def resolve_referenced_constants(
    idx, body: str, *, exclude_qname: str | None = None,
) -> dict[str, dict]:
    """Inline the literal values of any ``SCREAMING_SNAKE`` identifiers
    referenced in ``body`` that are themselves indexed constants.

    Built for the audit-class case: ``search --with-bodies`` returns a
    function body containing ``model=DEFAULT_TRANSLATION_MODEL``. Without
    this enrichment the agent has to follow up with another query to
    discover what ``DEFAULT_TRANSLATION_MODEL`` resolves to. With it,
    every such reference is pre-resolved (alias chain → terminal literal)
    and attached to the result.

    Returns ``{name: {qname, value[, chain]}}`` or ``{}`` if nothing
    matches. The lookup is a small constant per unique name and short-
    circuits when the symbols table doesn't have a constant with that
    name. Names that match the result's own qname tail are skipped so
    we don't re-resolve a constant against itself.
    """
    names = set(_UPPER_REF_RE.findall(body))
    if exclude_qname:
        tail = exclude_qname.rsplit(":", 1)[-1].rsplit(".", 1)[-1]
        names.discard(tail)
    if not names:
        return {}

    out: dict[str, dict] = {}
    for name in names:
        row = idx.conn.execute(
            "SELECT qname, signature FROM symbols WHERE kind = 'constant' "
            "AND (qname = ? OR qname LIKE ? OR qname LIKE ?) LIMIT 1",
            (name, f"%:{name}", f"%.{name}"),
        ).fetchone()
        if row is None:
            continue
        chain = resolve_constant_chain(idx, row["signature"], row["qname"])
        if chain is not None:
            out[name] = {
                "qname": row["qname"],
                "value": chain["value"],
                "chain": chain["chain"],
            }
        else:
            sig = row["signature"]
            value = sig.split("=", 1)[1].strip() if "=" in sig else sig
            out[name] = {"qname": row["qname"], "value": value}
    return out
