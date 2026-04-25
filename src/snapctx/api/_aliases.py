"""Cross-file constant-alias resolution.

When a constant is just an alias for another (``DEFAULT_MODEL = TRANSLATION_MODEL``),
``context()`` follows the chain so the agent sees the terminal literal value
without an extra round-trip. Lives in its own module because the chain-following
logic is meaningful enough to read in isolation.
"""

from __future__ import annotations

import re

_CONSTANT_ALIAS_RE = re.compile(r"^\s*[A-Z][A-Z0-9_]*\s*=\s*([A-Z][A-Z0-9_]*)\s*$")


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
