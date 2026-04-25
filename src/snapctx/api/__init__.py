"""Public API surface for snapctx.

Every operation lives in a focused submodule (``_search``, ``_graph``,
``_retrieve``, ``_context``, ``_indexer``, ``_multi``); this package
re-exports them so callers (CLI, tests, library users) can keep
importing from ``snapctx.api`` regardless of the internal layout.

Stable surface — anything imported here is expected to keep working:

* Operations: ``search_code``, ``expand``, ``outline``, ``get_source``,
  ``context``, ``index_root``.
* Multi-root variants: ``search_code_multi``, ``expand_multi``,
  ``outline_multi``, ``get_source_multi``, ``context_multi``.
"""

from snapctx.api._context import context
from snapctx.api._graph import expand
from snapctx.api._indexer import index_root
from snapctx.api._multi import (
    context_multi,
    expand_multi,
    get_source_multi,
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
    "expand",
    "expand_multi",
    "get_source",
    "get_source_multi",
    "index_root",
    "outline",
    "outline_multi",
    "search_code",
    "search_code_multi",
]
