# neargrep

[![CI](https://github.com/neargrep/neargrep/actions/workflows/ci.yml/badge.svg)](https://github.com/neargrep/neargrep/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python: 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

**Structured codebase context for AI agents.** One tool call replaces the agent's usual loop of `grep` + `read` + chase imports. Point it at a codebase; ask a natural-language question; get back a self-contained context pack — top symbols, their signatures, their docstrings, a **depth-2 call-path trace**, module-level architectural docstrings, and the full source of the top matches — in single-digit ms per query once warm.

Python, TypeScript, TSX, and JSX today. Parser layer is pluggable — more languages are straight additions.

---

## Why this exists

An agent investigating an unfamiliar codebase burns tokens the same way every time:

1. `Grep` — 50–500 hits, most irrelevant.
2. `Read` three candidate files — 10–30 k tokens of which 95 % is noise.
3. `Grep` again for a name the agent just saw.
4. `Read` two more files to chase imports.
5. Synthesize, sometimes with gaps.

On a real 1,500-symbol codebase a question like "how does the parallel reader fetch verses for multiple versions?" takes **15–25 tool calls**, **30–80 k agent tokens**, and **60–120 seconds** of end-to-end agent time.

`neargrep` answers the same question with **1 tool call** returning **2–5 k tokens** of structured context. That single call runs in **~5 ms warm** (inside a long-lived MCP server, ~400 ms from a cold CLI while the embedding model loads) — by doing the search → graph-walk → source-read → constant-alias resolution once, on a pre-built index, and returning a single structured payload the agent can reason about immediately. The agent's own reasoning time on top of that is still seconds, but the tool-call cost it pays drops by ~10× on calls and ~10× on tokens.

---

## Install

`neargrep` is a CLI tool. Install it globally with `uv tool` (isolated venv, entry points on `PATH`):

```bash
cd neargrep
uv tool install --editable .
```

That puts `neargrep` on your `PATH` (typically `~/.local/bin`).

Uninstall: `uv tool uninstall neargrep`.

### Requirements

- Python ≥ 3.11
- [`uv`](https://github.com/astral-sh/uv) for install (or use `pip install -e .` inside a venv)
- ~200 MB disk for the ONNX embedding model (downloaded on first index)

---

## 60-second quickstart

```bash
# 1. Index a repo. Creates <repo>/.neargrep/index.db
neargrep index /path/to/your/python/repo

# 2. Ask a question — one shot, get everything back
neargrep context "how does session authentication work" --root /path/to/your/python/repo
```

Output is JSON: top-5 matching symbols with full source for each, their callees and callers, and file outlines for the files involved. Usually enough to answer a non-trivial question without any follow-up.

For finer-grained operations (you'll rarely need them):

```bash
neargrep search  "rate limit"  --root /repo          # just top-k ranked symbols
neargrep outline src/auth.py   --root /repo          # nested symbol tree of a file
neargrep expand  "auth:login"  --direction callers   # who calls this?
neargrep source  "auth:login"  --with-neighbors      # body + callee signatures
```

All commands accept `--root` (defaults to the current directory).

---

## Use it from an AI agent (MCP)

`neargrep-mcp` is an MCP stdio server. Claude Code, Cursor, Cline, or any MCP client can call its five tools (`context`, `search`, `expand`, `outline`, `source`) natively in-session. The server loads the model once and each tool call runs in **5–10 ms warm**.

### 1. Register the server

Drop this in `.mcp.json` at your repo root:

```json
{
  "mcpServers": {
    "neargrep": {
      "command": "neargrep-mcp",
      "args": ["--root", "."]
    }
  }
}
```

Restart your MCP client. In Claude Code: `/mcp` should list `neargrep` with all five tools.

### 2. Tell the agent to use it first

Agents default to `Grep` + `Read` out of habit. Paste this into your project's `CLAUDE.md` / `AGENTS.md` so the agent actually reaches for neargrep:

```markdown
## Code exploration

This repo is indexed by `neargrep` (MCP server registered in `.mcp.json`). When you need to understand unfamiliar code, prefer these tools over `Grep`/`Read`:

**First move for any code-understanding question: `mcp__neargrep__context`.** Pass a natural-language query or identifier. Returns top symbols with source, a depth-2 call-path trace (callees-of-callees + callers-of-callers), and file outlines — typically enough to answer without follow-up.

**Drill-down** (only when `context` wasn't enough):
- `mcp__neargrep__search` — ranked symbols; args: query, k, mode, kind.
- `mcp__neargrep__expand` — call-graph neighborhood; args: qname, direction.
- `mcp__neargrep__outline` — file symbol tree; args: path.
- `mcp__neargrep__source` — full body of one symbol; args: qname.

Fall back to `Grep` only for: URL route strings, TODO comments, filename patterns. neargrep indexes symbols, not raw text.

If you get a "no index" error: run `neargrep index <repo-root>` first.
```

See **[`docs/agent-setup.md`](docs/agent-setup.md)** for the full guide: other MCP clients, troubleshooting, per-project tuning.

### 3. Verify

```bash
# Smoke-test the MCP server
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"t","version":"0"}}}' | neargrep-mcp --root /path/to/repo --no-warm
```

Expect a JSON response with `"serverInfo": {"name": "neargrep", "version": "0.1.0"}`.

### 4. First-run approval (Claude Code)

On the first launch after `.mcp.json` appears, Claude Code prompts *"New MCP server found in .mcp.json: neargrep"* with three options. Pick **"Use this and all future MCP servers in this project"** (remembers approval for any future `.mcp.json` entries you add) or **"Use this MCP server"** (just this one). If you dismiss with `Esc`, Claude Code records it as skipped and `/mcp` will quietly omit the server until you edit `.mcp.json` (changing its mtime) and restart.

---

## Security model

The MCP server is a read-heavy local tool with a tight sandbox. Worth understanding before approving the trust prompt.

**What it reads:**
- Files under the `--root` you passed in `.mcp.json`, and only files the walker *indexed*:
  - Python source only (`.py`, `.pyi`) — **not** `.env`, credentials, logs, binaries.
  - Respects `.gitignore` — anything excluded there is invisible.
  - Skips `.git`, `.venv`, `node_modules`, `__pycache__`, `.neargrep`, `dist`, `build`, `.tox` by default.

**What it writes:**
- Exactly one place: `<root>/.neargrep/index.db` (+ WAL/SHM sidecars). Nothing else on disk is ever touched.

**Network:**
- None at runtime. The ONNX embedding model is downloaded once on first `neargrep index` (into `~/.cache/huggingface/`), then every future query runs fully offline.

**Subprocess / code execution:**
- None. The server doesn't shell out, doesn't `eval`, doesn't spawn Python subprocesses. It parses files with the stdlib `ast` module, runs SQLite queries, and does ONNX inference.

**Path-traversal protection:**
- `--root` is resolved once at server startup and baked into every tool call — the agent cannot override it.
- `outline(path=…)` only returns symbols that exist in the index. A request for `/etc/passwd` or any other off-root path returns an empty result; no filesystem read happens.
- `get_source(qname)` reads the file path stored on the matched symbol row, which the walker guarantees is under `--root`.

**The one real caveat — same as `Read` / `Grep`:**
Any secret hardcoded into a `.py` file (API keys in module constants, credentials baked into source) becomes discoverable via the semantic search. `Read` and `Grep` already expose that; neargrep just makes it *more* findable. Don't commit secrets. If legacy code has them, add the file to `.gitignore`.

**Summary:** exposure = same set of files your agent could already `Read`/`Grep`, minus all non-Python content, minus anything in `.gitignore`. No network, no exec, no writes outside `.neargrep/`.

---

## How it works

### Indexing (one-time per repo, re-runs incrementally)

`neargrep index <root>` walks the repo respecting `.gitignore` and per-extension parsers, then builds three artifacts in `<root>/.neargrep/`:

1. **`symbols` table** — every function, method, class, nested closure, interface, type alias, React component, module (with file-level docstring or leading JSDoc block), and module-level or class-level constant. Fields: qualified name (`module.path:Class.method`), kind, signature, docstring, file, line range, decorators, base classes, source SHA.
2. **`calls` table** — caller → callee edges. Callee names are heuristically resolved against the caller's import table, with optimistic resolution through base-class MRO for `self.X` method calls. Calls in decorator arguments and default values are attributed to the module, not the decorated function (they run at definition time, not per-call). After full ingest, two post-passes fix up edges:
   - **Demote** — nulls any callee qname that didn't land on a real symbol (so the agent doesn't chase dead `Model.objects.filter`-style chains).
   - **Promote** — resolves forward-referenced `self.X()` calls where the target method was defined later in the same class body than the caller.
3. **`symbols_fts`** (SQLite FTS5 virtual table) + **`symbol_vectors`** (384-dim `bge-small-en-v1.5` embeddings) — for search.

Incremental: files whose SHA matches the stored value are skipped. Only changed files get re-parsed and re-embedded.

The walker skips vendored/bundled assets by default: `node_modules`, `.venv`, `dist`, `vendor`, `bower_components`, `*.min.js`, `*.bundle.js`, `*-bundle.js`, `*.standalone.js`, `*.lib.js`, `*.worker.js`, `*.map`, plus any source file over 250 KB (real hand-written code stays well under this ceiling; bundles never do).

**Cost on a real mixed-language monorepo (754 files, 3,290 symbols — Python Django backend + Next.js frontend):**
- Full index from scratch: **~10 seconds** (cold, including ONNX model load)
- Re-index after editing one file: <200 ms (SHA-skip the rest)
- Query-time `context()` with depth-2 call trace: **~380 ms** cold, **5–10 ms** warm

### Query-time: the four ops

All four ultimately read from the same SQLite file. They're cheap and composable; `context` is the all-in-one wrapper.

**`search_code(query, k=5, kind?, mode="hybrid")`** — Rank symbols against a query.

- `mode="lexical"` — SQLite FTS5 / BM25 over qnames (camel + snake split), signatures, docstrings, decorators. ~2 ms warm. Great for keyword matches; misses paraphrase.
- `mode="vector"` — cosine similarity over bge-small-en-v1.5 embeddings of each symbol. ~5 ms warm. Great for paraphrased / conceptual queries ("rate limit" → `throttle_requests`); a little noisier at the tail.
- `mode="hybrid"` (default) — weighted Reciprocal Rank Fusion of both lists:
  `rrf(q) = Σ weight / (60 + rank)`
  with `vec_weight=1.5`, `lex_weight=1.0`, plus a `test_penalty=0.6` multiplier for symbols living under `tests/` (so test methods don't out-rank real code).

**`expand(qname, direction, depth)`** — Walk the call graph. Returns signatures + docstring summaries of neighbors, not bodies. `direction` is `callees`, `callers`, or `both`. This is how the agent traces "where is this used?" without reading whole files.

**`outline(path)`** — File's symbol tree, nested by containment. Includes class-level constants (Django `STATUS_CHOICES`-style) — often the most token-efficient way to see what a file exports.

**`get_source(qname, with_neighbors=False)`** — Full source of a single symbol. With `with_neighbors=True`, appends compact signatures for each resolved callee.

### Query-time: `context(query)` — the one-shot op

This is the operation most agents should call first. It bundles everything the agent is likely to need into a single response:

1. Runs `search_code` with the chosen mode (default: `hybrid`).
2. **Fast path:** if the query contains `:` and exactly matches a known qname, the search is skipped entirely. The pack is built around that single symbol in ~1 ms.
3. For each of the top `k_seeds=5` hits:
   - Signature, docstring, file, line range, decorators, score.
   - **Depth-2 call trace** — up to `neighbor_limit=8` direct callees, each resolved callee carrying up to `max(3, neighbor_limit//2)` of its own callees. Same shape for callers. An agent sees the full flow (e.g. `Runner.emit → _publish_delta → client.publish`) without a follow-up `expand` call. Unresolved calls to Python builtins (`print`, `len`, `isinstance`, …) are filtered so the graph stays focused on domain code.
   - Full source body (capped at `body_char_cap=2000` chars each), for all top `source_for_top=5` seeds.
   - **Constant-alias resolution**: if the seed is `NAME = OTHER_NAME`, the chain is followed (up to 3 hops, cross-file) and the terminal literal is attached as `resolved_value`. So the agent sees the real string (`'claude-opus-4-5'`) without a separate `source` call on the registry module.
4. File outlines for up to 8 unique files among the search candidates (the pool is overfetched beyond the top-K seeds so survey questions get full coverage).

Typical output is 3–8 k tokens. Warm in-process latency is 5–10 ms.

### Ranking details & why hybrid won

We raced three modes on a real 1,600-symbol repo across two question types:

**Q1 — focused ("what fields does `TranslationVerse` have?"):**

| Mode | Calls | Tokens | Duration | Accurate? |
|---|---:|---:|---:|:---:|
| lexical | 4 | 23 k | 29 s | ✓ |
| hybrid | 4 | 21 k | 21 s | ✓ |
| **`context()`** | **1** | 24 k | **17 s** | **✓** |

**Q5 — survey ("list every LLM provider + model + phase"):**

Numbers are **agent-level totals** (tool calls + tokens the agent consumed across the whole question + wall-clock end-to-end). Each row is a fresh blind-eval subagent.

| Mode | Tool calls | Agent tokens | Duration | Notes |
|---|---:|---:|---:|---|
| lexical (no constants) | 28 | 80 k | 112 s | missed `DEFAULT_*_MODEL` constants |
| lexical + constants | 16 | 54 k | 63 s | complete |
| vector-only | 6 | 44 k | 38 s | complete, some tail noise |
| composable hybrid | 15 | 41 k | 57 s | most thorough |
| **`context()`** | **1** | 42 k | **30 s** | **complete with alias resolution** |

Caveat: all blind-eval agents made one-off CLI calls (each paying ~400 ms cold-start). In an MCP setup where the server stays warm, the per-call latency drops to 5 ms — but the agent's own reasoning time (generating the follow-up calls it *didn't* need to make, writing the answer) is the same, so the wall-clock win for `context()` comes primarily from replacing **15 reasoning-and-call round-trips with 1**, not from making a single call faster.

Highlights from the lexical-vs-vector head-to-head:

- *"TranslationVerse fields"* — lexical's top-3 were all **test methods** (BM25 camel-split matched `field_exists`). Vector got the real class. Hybrid with test-penalty matches vector here.
- *"which embedding model do we use"* — lexical matched on `we_process_batch` (wrong). Vector got `EmbeddingProvider`. Hybrid got the right answer.
- *"anthropic claude translation"* — all three worked. Hybrid was the most comprehensive (agent class + every DEFAULT_* constant).

**Why RRF, not score fusion?** FTS5 BM25 scores and cosine similarities live in different units. Normalizing is lossy and config-sensitive. RRF uses ranks alone and is robust — the only knob is the weight ratio, which we tuned once on real queries.

### Performance

Measured on a mixed-language monorepo — Python Django backend + Next.js frontend, 754 files, 3,290 symbols — on an M-series Mac.

| Path | Latency |
|---|---:|
| Cold index from scratch (parse + embed all symbols) | ~10 s |
| Re-index after one-file edit (SHA-skip the rest) | <200 ms |
| Cold CLI, hybrid `context()` call | ~380 ms |
| Cold CLI, exact qname (fast path) | ~30 ms |
| Warm in-process, hybrid `context()` (depth-2 trace) | **5–10 ms** |
| Warm in-process, exact qname | **1–2 ms** |

The cold-to-warm delta is almost entirely the fastembed ONNX model load. Inside an MCP server, the file watcher, or any long-running process, the model loads once and every subsequent query runs in single-digit ms.

Indexing is I/O- and embedding-bound. The three levers we tuned (on the way from 85 s → 10 s on the same repo): walker-level vendor-bundle filter, TS signature truncation to 240 chars so massive `const X: ColumnDef<T>[] = [...]` declarations don't bloat the index, and `fastembed` batch size = 4 (counter-intuitive, but smaller batches mean less ONNX padding waste on mixed-length texts).

---

## The full API

Everything the CLI exposes is also available as a Python library. Import from `neargrep.api`:

```python
from neargrep.api import context, search_code, expand, outline, get_source, index_root

# Build or refresh the index
index_root("/path/to/repo")

# One-shot context pack (what the agent should call first)
pack = context("how does session authentication work", root="/path/to/repo")
for seed in pack["seeds"]:
    print(seed["qname"], seed["signature"])
    if "resolved_value" in seed:
        print("  → ", seed["resolved_value"]["value"])

# Composable operations, if the agent wants to drill down
hits = search_code("throttle", k=3, mode="vector", root="/path/to/repo")
neighbors = expand("auth.service:login", direction="callers", root="/path/to/repo")
tree = outline("src/auth/service.py", root="/path/to/repo")
src = get_source("auth.service:login", with_neighbors=True, root="/path/to/repo")
```

All functions return JSON-serializable dicts.

---

## Response shape reference

### `context` response (abridged)

```jsonc
{
  "query": "…",
  "mode": "hybrid",           // or "lexical" | "vector" | "exact"
  "seeds": [
    {
      "rank": 1,
      "qname": "module.path:ClassName.method",
      "kind": "method",        // function | method | class | module | constant | …
      "signature": "def method(self, arg: T) -> R",
      "docstring": "One-line summary.",
      "file": "/abs/path/file.py",
      "lines": "42-67",
      "score": 0.0381,
      "decorators": ["@property"],
      "callees": [
        {
          "qname": "other:helper",
          "signature": "def helper(x)",
          "docstring": "…",
          "line": 55,
          "callees": [                        // depth-2 nested hop
            { "qname": "util:sanitize", "signature": "def sanitize(x)", "line": 12 }
          ]
        }
      ],
      "callers": [ /* same shape, with nested "callers" at depth 2 */ ],
      "source": "def method(self, arg: T) -> R:\n    …",
      "resolved_value": {      // only for constant-alias seeds
        "chain": ["defaults:DEFAULT_MODEL"],
        "terminal_qname": "defaults:DEFAULT_MODEL",
        "value": "'claude-opus-4-5'"
      }
    }
  ],
  "file_outlines": [
    {
      "file": "/abs/path/file.py",
      "symbols": [ { "qname": "…", "kind": "…", "signature": "…", "lines": "10-30" } ]
    }
  ],
  "token_estimate": 3046,
  "hint": "If it's still not enough, call expand/outline/source on a specific qname."
}
```

### `search_code` response

```jsonc
{
  "query": "…",
  "mode": "hybrid",
  "results": [
    {
      "qname": "…",
      "kind": "…",
      "signature": "…",
      "docstring": "…",
      "file": "…",
      "lines": "…",
      "score": 0.037,
      "next_action": "expand"    // expand | outline | read_body | enough
    }
  ],
  "hint": "Call expand('<qname>') to see what this depends on."
}
```

`next_action` is the tool's opinion about what the agent should do next with the top hit. Classes → `outline`. Functions with short docstrings → `read_body`. Otherwise → `expand`.

---

## What's indexed, what's not

**Indexed:**
- **Python:** functions and methods (`def`, `async def`, including nested / closure definitions); classes (with base-class list for MRO-aware `self.X` resolution); module-level constants (`UPPER_CASE = literal | identifier | collection`); class-level constants (`class C: STATUS_CHOICES = [...]`); imports (for call-site resolution).
- **TypeScript / TSX / JSX:** functions and arrow-const functions; classes (with `extends` / `implements` base chain); interfaces and type aliases; enums; React components (capitalized name + JSX in body); module-scope typed constants. JSX usage is tracked as a call edge (`<Button />` → `Button:Button`).
- **Module docstrings** — Python files with a top string docstring and TS/JSX files opening with a `/** … */` block. This captures the architectural "why" of a file, which often isn't repeated on any single class or function.
- **Call edges**, with optimistic `self.X` resolution through base classes, plus a post-ingest *promote* pass that fixes up forward references (methods defined later in the class body than their caller).

**Not indexed (by design):**
- Strings inside source files (so `urlpatterns = [...]` route strings are not searchable).
- Comment blocks (except file-leading JSDoc).
- Runtime-dynamic symbols (metaclasses, `type(...)` factories, monkeypatched attributes).
- Bundled / vendored / minified JS (walker skips files over 250 KB and common bundle suffixes like `*-bundle.js`, `*.lib.js`, `*.standalone.js`, `*.worker.js`, `*.map`).
- Calls that run at module-load time, not runtime — decorator arguments, default values, and type annotations aren't attributed as the enclosing function's callees.

**Known rough edges:**
- `self.x.y.z` attribute chains are left unresolved by design — guessing is worse than saying "don't chase this".
- Django ORM-style `Model.objects.filter(...)` is demoted to unresolved after the post-ingest sweep (the chain doesn't point at real symbols).
- Class hierarchies where the base lives behind a runtime import or dynamic `__init_subclass__` will miss MRO edges.
- TypeScript callee resolution is limited without a full type system: calls on parameters, local variables, imported constants, and `this.*` are often unresolved. Depth-2 traces are most useful on Python code paths.

---

## Project layout

```
neargrep/
  pyproject.toml
  src/neargrep/
    __init__.py
    schema.py           # Symbol / Call / Import / ParseResult dataclasses
    qname.py            # qname formatting + camel/snake splitting
    parsers/
      base.py           # Parser protocol (language-agnostic)
      python.py         # stdlib ast implementation
      typescript.py     # tree-sitter TS + TSX grammars (JSX-aware)
      registry.py       # extension → parser dispatch
    walker.py           # gitignore-aware file walker + bundle/size filters
    index.py            # SQLite schema, FTS5, vector BLOB, promote/demote passes
    embeddings.py       # fastembed + bge-small-en-v1.5
    watch.py            # debounced file-watcher for auto re-index on save
    api.py              # search_code, expand, outline, get_source, context, index_root
    cli.py              # argparse entry point (index, search, expand, outline,
                        #                       source, context, watch)
    adapters/
      mcp.py            # MCP stdio server for Claude Code / Cursor / Cline
  tests/
    fixtures/sample_pkg/
    test_parser.py  test_qname.py  test_api.py  test_constants.py
    test_demotion.py  test_embeddings.py  test_context.py  test_context_alias.py
    test_incremental.py  test_mcp_adapter.py  test_typescript_parser.py
    test_watch.py
```

Add a new language by implementing `parsers/base.Parser` for its extension and registering it in `parsers/registry`. The SQLite schema, ranking, `context()` logic, and post-ingest promote/demote passes are all language-agnostic — the TypeScript parser slotted in without any changes outside its own file.

---

## Running the test suite

```bash
# from the repo root
uv pip install --group dev      # or: uv pip install pytest
pytest
```

70 tests currently pass. First run downloads the ONNX embedding model (~30 MB, cached under `~/.cache/huggingface/`).

---

## Status and roadmap

**Shipped — validated on a mixed-language monorepo (Django + Next.js, 754 files / 3,290 symbols):**
- [x] Python AST extraction — functions, methods, classes, nested scopes, module-level + class-level constants, **module docstrings**
- [x] TypeScript / TSX / JSX parser (tree-sitter) — functions, arrow consts, classes, interfaces, type aliases, enums, React components, constants, imports, JSX usage as call edges, **leading JSDoc module docs**
- [x] Call graph with MRO-aware `self.X` resolution, plus post-ingest **promote** pass for forward references
- [x] Decorator-arg / default-value / type-annotation calls filtered out of runtime call graph
- [x] SQLite + FTS5 lexical search
- [x] `bge-small-en-v1.5` embeddings + cosine vector search
- [x] Weighted RRF hybrid ranker with test-file demotion
- [x] One-shot `context()` — **depth-2 call-path trace**, constant-alias resolution, multi-file outlines from an overfetched candidate pool
- [x] Builtin-noise filter for unresolved callees (`?:print`, `?:len`, `?:isinstance`, …)
- [x] Fast path for exact-qname queries (~1 ms warm)
- [x] Incremental indexing (SHA-based)
- [x] Walker vendor-bundle / size filter — skips minified JS, source maps, `*-bundle.js`, `*.lib.js`, `*.standalone.js`, and anything over 250 KB
- [x] **MCP stdio adapter** (`neargrep-mcp`) — exposes all five ops to Claude Code / Cursor / Cline via `.mcp.json`
- [x] **File watcher** (`neargrep watch`) — debounced auto re-index on save, typical run ~5 ms warm

**Planned next:**
- [ ] **TS scope tracker** — parameter / local / import resolution so TS callee traces aren't stuck at depth 1.
- [ ] **Google ADK adapter** — thin `FunctionTool` wrappers over `neargrep.api` for in-process ADK agents.
- [ ] **`neargrep serve` daemon** — holds model + DB warm so even cold-CLI calls are single-digit ms.
- [ ] **Adaptive ranker** — stopword-filter / vector-weight bump for long natural-language queries; lexical-heavy for short identifier lookups.

---

## License

MIT.
