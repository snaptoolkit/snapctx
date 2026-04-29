#!/usr/bin/env python3
"""Bridge between opencode tool wrappers and the snapctx Python API.

Reads a single JSON object from stdin: ``{"op": "<name>", "args": {...},
"root": "<cwd>"}``. Dispatches to ``snapctx.api.<op>`` and prints the
result as JSON to stdout. Exits non-zero with an error JSON on failure.

Used because most snapctx write ops (delete_symbol, edit_symbol_batch,
add_import, etc.) are Python-API-only — not exposed via the CLI.

Requires: a Python interpreter with ``snapctx`` installed.
Override which interpreter the wrapper uses via the
``SNAPCTX_PYTHON`` environment variable; if unset, the wrapper falls
back to ``python3`` on PATH.
"""
from __future__ import annotations

import json
import sys
import traceback


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        op = payload["op"]
        args = payload.get("args", {})
        root = payload.get("root", ".")

        try:
            from snapctx import api
        except ImportError as e:
            print(json.dumps({
                "error": "snapctx_not_installed",
                "hint": (
                    "Install snapctx in this Python (`pip install snapctx`) "
                    "or set SNAPCTX_PYTHON to an interpreter that has it."
                ),
                "detail": str(e),
            }))
            return 3

        fn = getattr(api, op, None)
        if fn is None or not callable(fn):
            print(json.dumps({"error": f"unknown op: {op}"}))
            return 2

        # Mirror the CLI's pre-query auto-refresh so session-tool reads
        # also pick up file changes and parser-version upgrades. Without
        # this, an old index built before a parser bump would keep
        # serving stale rows for read ops (search/source/expand) until
        # the user happened to invoke a write op or the CLI directly.
        # Skipped for ``index_root`` itself (would recurse) and for
        # vendor-scoped paths the API already guards.
        if op != "index_root":
            try:
                api.index_root(root)
            except Exception:
                # Refresh failures shouldn't take down the actual op —
                # the dispatched fn will surface its own error if the
                # index is genuinely unusable.
                pass

        # Every snapctx write op accepts ``root=`` as a kwarg.
        result = fn(root=root, **args)
        print(json.dumps(result, default=str))
        return 0
    except Exception as e:
        print(json.dumps({
            "error": str(e),
            "type": type(e).__name__,
            "trace": traceback.format_exc(),
        }))
        return 1


if __name__ == "__main__":
    sys.exit(main())
