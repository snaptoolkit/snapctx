"""MCP stdio adapter for snapctx.

Exposes snapctx's five context operations as MCP tools. Intended to be
launched by an MCP-speaking client (Claude Code, Cursor, Cline, custom Agent
SDK loops) as a subprocess. The server stays resident for the session so
the embedding model and SQLite handle are loaded once — every query after
the first runs in 5–8 ms.

Start:
    snapctx-mcp --root /path/to/indexed/repo

Typical client config (e.g. ``.mcp.json`` in a project):

    {
      "mcpServers": {
        "snapctx": {
          "command": "snapctx-mcp",
          "args": ["--root", "."]
        }
      }
    }

The working directory of ``.`` is interpreted by the client; for Claude Code
it resolves to the project root.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import mcp.types as types
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server

from snapctx.api import context, expand, get_source, outline, search_code
from snapctx.index import db_path_for


# --- tool descriptions ------------------------------------------------------
# These are what the model reads to decide *which* tool to call. They should
# be specific about when to prefer this over Grep/Read, and include concrete
# example query shapes.

CONTEXT_DESC = """\
Gather structured context about an unfamiliar codebase in ONE call.

PREFER THIS over Grep/Read as your FIRST action when exploring unfamiliar code.

Bundles top-5 matching symbols (with signatures, docstrings, and full source bodies), their immediate callees and callers, and file outlines for each file involved. Usually enough to answer a non-trivial question without any follow-up call.

Query shapes:
- Natural language: "how does session authentication work", "where are rate limits applied".
- Identifiers / partial: "verify_credentials", "SessionManager".
- Exact qname (fast path, ~1 ms): "auth.session:SessionManager.refresh".

Module-level constants that alias other constants (e.g. DEFAULT_MODEL = DEFAULT_TRANSLATION_MODEL) are auto-resolved to their terminal literal value — you see the real "claude-opus-4-5" string without a follow-up call.

Returns JSON with: seeds[] (ranked symbols w/ source + neighbors), file_outlines[], token_estimate, hint. Typical response 3–10 k tokens. Latency ~8 ms warm.
"""

SEARCH_DESC = """\
Ranked search over indexed symbols (functions, methods, classes, constants).

Use when you already know what you want and `context` would be overkill. Returns top-k symbols with signatures, docstring summaries, scores, and a `next_action` hint.

Modes:
- hybrid (default): weighted RRF of BM25 + bge-small embeddings; robust across query styles.
- lexical: pure FTS5, fast (<2 ms warm), good for exact identifiers.
- vector: pure embeddings, good for paraphrase (e.g. "rate limit" matches `throttle_requests`).

Use `kind` to filter: function | method | class | module | constant.
"""

EXPAND_DESC = """\
Walk the call graph around a qualified name. Returns neighbor SIGNATURES (not bodies).

Use to trace "who calls this?" or "what does this depend on?" without reading files.

Direction:
- callees: functions this qname invokes.
- callers: functions that invoke this qname.
- both: union.

Self-method calls resolve through the enclosing class's base classes (MRO-aware), so mixin methods are picked up even when they live in a different file.
"""

OUTLINE_DESC = """\
Symbol tree of a single file. Nested by containment; includes module-level and class-level constants.

More token-efficient than reading the whole file when you just want to know what a module exports. Path can be absolute or relative to the indexed root.
"""

SOURCE_DESC = """\
Full source body for one qualified name.

Call this only when `context`'s bundled source wasn't enough and you need the exact code of a specific symbol. Optionally include a compact list of the symbol's resolved callees (signature + docstring).
"""


# --- server -----------------------------------------------------------------


def make_server(root: Path) -> Server:
    server: Server = Server("snapctx")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name="context",
                description=CONTEXT_DESC,
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Natural-language question or identifier.",
                        },
                        "k_seeds": {"type": "integer", "default": 5, "minimum": 1, "maximum": 15},
                        "source_for_top": {"type": "integer", "default": 5, "minimum": 0, "maximum": 15},
                        "mode": {
                            "type": "string",
                            "enum": ["lexical", "vector", "hybrid"],
                            "default": "hybrid",
                        },
                        "kind": {
                            "type": "string",
                            "enum": ["function", "method", "class", "module", "constant"],
                            "description": "Optional symbol-kind filter.",
                        },
                    },
                    "required": ["query"],
                },
            ),
            types.Tool(
                name="search",
                description=SEARCH_DESC,
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "k": {"type": "integer", "default": 5, "minimum": 1, "maximum": 25},
                        "mode": {
                            "type": "string",
                            "enum": ["lexical", "vector", "hybrid"],
                            "default": "hybrid",
                        },
                        "kind": {
                            "type": "string",
                            "enum": ["function", "method", "class", "module", "constant"],
                        },
                    },
                    "required": ["query"],
                },
            ),
            types.Tool(
                name="expand",
                description=EXPAND_DESC,
                inputSchema={
                    "type": "object",
                    "properties": {
                        "qname": {
                            "type": "string",
                            "description": "Qualified name, e.g. 'myapp.auth:SessionManager.refresh'.",
                        },
                        "direction": {
                            "type": "string",
                            "enum": ["callees", "callers", "both"],
                            "default": "callees",
                        },
                        "depth": {"type": "integer", "default": 1, "minimum": 1, "maximum": 3},
                    },
                    "required": ["qname"],
                },
            ),
            types.Tool(
                name="outline",
                description=OUTLINE_DESC,
                inputSchema={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Path to a source file, absolute or relative to the indexed root.",
                        },
                    },
                    "required": ["path"],
                },
            ),
            types.Tool(
                name="source",
                description=SOURCE_DESC,
                inputSchema={
                    "type": "object",
                    "properties": {
                        "qname": {"type": "string"},
                        "with_neighbors": {
                            "type": "boolean",
                            "default": False,
                            "description": "Also return a compact list of this symbol's resolved callees.",
                        },
                    },
                    "required": ["qname"],
                },
            ),
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
        args = dict(arguments or {})
        try:
            if name == "context":
                result = context(root=root, **args)
            elif name == "search":
                result = search_code(root=root, **args)
            elif name == "expand":
                result = expand(root=root, **args)
            elif name == "outline":
                result = outline(root=root, **args)
            elif name == "source":
                result = get_source(root=root, **args)
            else:
                result = {"error": f"unknown tool: {name}"}
        except TypeError as e:
            result = {"error": f"bad arguments for {name}: {e}"}
        except Exception as e:
            result = {"error": f"{type(e).__name__}: {e}"}
        return [types.TextContent(type="text", text=json.dumps(result, indent=2))]

    return server


async def run_async(root: Path) -> None:
    server = make_server(root)
    async with stdio_server() as (read_stream, write_stream):
        init_opts = InitializationOptions(
            server_name="snapctx",
            server_version="0.1.0",
            capabilities=server.get_capabilities(
                notification_options=NotificationOptions(),
                experimental_capabilities={},
            ),
        )
        await server.run(read_stream, write_stream, init_opts)


def main() -> None:
    parser = argparse.ArgumentParser(prog="snapctx-mcp", description="snapctx MCP stdio server")
    parser.add_argument(
        "--root",
        default=".",
        help="Indexed repo root (defaults to CWD).",
    )
    parser.add_argument(
        "--no-warm",
        action="store_true",
        help="Skip pre-warming the embedding model at startup (saves ~250 ms startup, first query pays it).",
    )
    args = parser.parse_args()
    root = Path(args.root).resolve()

    db = db_path_for(root)
    if not db.exists():
        print(
            f"[snapctx-mcp] warning: no index at {db}.\n"
            f"                   Run `snapctx index {root}` once before querying.",
            file=sys.stderr,
        )

    if not args.no_warm:
        # Load the embedder now so the first MCP tool call runs in single-digit ms.
        from snapctx.embeddings import embed_texts

        embed_texts(["warmup"])

    asyncio.run(run_async(root))


if __name__ == "__main__":
    main()
