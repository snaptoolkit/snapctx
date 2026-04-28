"""Public API surface for snapctx.

Every operation lives in a focused submodule (``_search``, ``_graph``,
``_retrieve``, ``_context``, ``_indexer``, ``_multi``); this package
re-exports them so callers (CLI, tests, library users) can keep
importing from ``snapctx.api`` regardless of the internal layout.

Stable surface — anything imported here is expected to keep working:

* Read operations: ``search_code``, ``expand``, ``outline``,
  ``get_source``, ``find_literal``, ``map_repo``, ``context``.
* Write operations: ``edit_symbol`` (replaces a symbol body by qname).
* Indexing: ``index_root``.
* Multi-root variants: ``*_multi`` for each operation that supports
  fan-out / route-by-qname.
"""

from snapctx.api._context import context
from snapctx.api._edit import edit_symbol
from snapctx.api._find import find_literal
from snapctx.api._graph import expand
from snapctx.api._indexer import index_root
from snapctx.api._map import map_repo
from snapctx.api._multi import (
    context_multi,
    edit_symbol_multi,
    expand_multi,
    find_literal_multi,
    get_source_multi,
    map_repo_multi,
    outline_multi,
    search_code_multi,
)
from snapctx.api._retrieve import get_source, outline
from snapctx.api._search import search_code

# Internal helpers some tests still reach into. Kept public so removing
# them is a deliberate decision.
from snapctx.api._graph import is_builtin_noise as _is_builtin_noise  # noqa: F401
from snapctx.api._ranking import classify_query as _classify_query  # noqa: F401

__all__ = [
    "context",
    "context_multi",
    "edit_symbol",
    "edit_symbol_multi",
    "expand",
    "expand_multi",
    "find_literal",
    "find_literal_multi",
    "get_source",
    "get_source_multi",
    "index_root",
    "map_repo",
    "map_repo_multi",
    "outline",
    "outline_multi",
    "search_code",
    "search_code_multi",
]
