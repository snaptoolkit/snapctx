"""Shared helpers used across the api package.

These are the low-level bits every higher-level operation needs:
opening the index, formatting a row, summarizing a docstring, parsing
a "L1-L2" line range, and approximating token counts. Kept in one
place so each operation module stays focused on its actual logic.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from snapctx.index import Index, db_path_for


def open_index(root: Path, scope: str | None = None) -> Index:
    """Open the index for ``root`` (or one of its vendor packages).

    ``scope=None`` opens the repo's own index. A non-None scope is the
    name of a vendor package — opens
    ``<root>/.snapctx/vendor/<scope>/index.db`` instead. Raises a
    friendly error when the file doesn't exist; for vendor scopes the
    caller typically calls ``vendor.ensure_vendor_indexed`` first.
    """
    db = db_path_for(root, scope=scope)
    if not db.exists():
        if scope is None:
            raise FileNotFoundError(
                f"No index at {db}. Run `snapctx index {root}` first."
            )
        raise FileNotFoundError(
            f"No vendor index at {db}. The package {scope!r} hasn't been "
            f"indexed yet — run a query prefixed with `{scope}:` to "
            f"ingest it on demand."
        )
    return Index(db)


def row_to_symbol_dict(row: sqlite3.Row, *, include_body_line_range: bool = True) -> dict:
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


def docstring_summary(docstring: str | None) -> str | None:
    """Return just the first sentence/line of a docstring — sized for search results."""
    if not docstring:
        return None
    return docstring.strip().splitlines()[0]


def parse_line_range(lines: str) -> tuple[int, int]:
    if "-" in lines:
        a, b = lines.split("-", 1)
        return int(a), int(b)
    n = int(lines)
    return n, n


def rough_token_count(payload: dict) -> int:
    """Approximate token count as chars/4 over the payload's JSON rendering."""
    return len(json.dumps(payload)) // 4


# File extensions LLMs commonly leave on a qname's module portion when
# paraphrasing — e.g. ``components/Verse.tsx:Verse`` instead of the
# canonical ``components/Verse:Verse``. Stripped during qname resolution.
_QNAME_PARAPHRASE_EXTS = (".tsx", ".ts", ".jsx", ".js", ".pyi", ".py")


def resolve_qname(idx, qname: str) -> tuple[str | None, str | None]:
    """Return ``(canonical_qname, paraphrase_hint)`` if ``qname`` (or a
    plausible paraphrase of it) exists in the index, else ``(None, None)``.

    The "paraphrase hint" is a short human-readable description of the
    transformation we applied — None when ``qname`` was already canonical.
    Used to teach the caller (often an LLM) the correct format next time.

    Paraphrases handled:

    * **Stale extension** — LLMs sometimes keep ``.tsx``/``.ts``/``.py``
      on the module portion of a TS/Python qname. We strip those and retry.
    * **Path-style swap** — LLMs trained on Python qnames sometimes apply
      dotted-style to TS files (``components.Verse:Verse``); LLMs trained
      on TS qnames sometimes apply slashed-style to Python files. We
      try the opposite separator and retry.

    Order: exact → strip-extension → swap-separators → strip-and-swap.
    First hit wins. None of the retries are recursive; cost is O(4)
    extra index lookups in the not-found path.
    """
    # 1. Exact match — fastest path.
    if idx.get_symbol(qname) is not None:
        return qname, None

    if ":" not in qname:
        return None, None
    module, _, symbol = qname.partition(":")

    candidates: list[tuple[str, str]] = []

    # 2. Strip a stale extension from the module portion.
    for ext in _QNAME_PARAPHRASE_EXTS:
        if module.endswith(ext):
            stripped = module[: -len(ext)]
            candidates.append((f"{stripped}:{symbol}", f"stripped {ext!r} from module"))
            break

    # 3. Swap path separators in the module portion.
    if "/" in module and "." not in module.split("/")[0]:
        candidates.append(
            (f"{module.replace('/', '.')}:{symbol}", "converted '/' → '.' in module")
        )
    if "." in module and "/" not in module:
        candidates.append(
            (f"{module.replace('.', '/')}:{symbol}", "converted '.' → '/' in module")
        )

    # 4. Combine: strip extension AND swap separators.
    for cand, hint in list(candidates):
        for ext in _QNAME_PARAPHRASE_EXTS:
            cand_module = cand.partition(":")[0]
            if cand_module.endswith(ext):
                cand2 = f"{cand_module[: -len(ext)]}:{symbol}"
                candidates.append((cand2, f"{hint} + stripped {ext!r}"))
                break

    for cand, hint in candidates:
        if idx.get_symbol(cand) is not None:
            return cand, hint

    return None, None
