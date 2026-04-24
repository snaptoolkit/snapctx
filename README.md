# neargrep

[![CI](https://github.com/neargrep/neargrep/actions/workflows/ci.yml/badge.svg)](https://github.com/neargrep/neargrep/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python: 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

**Structured codebase context for AI agents.** One tool call replaces the agent's usual loop of `grep` + `read` + chase imports. Point it at a codebase; ask a natural-language question; get back a self-contained context pack — top symbols, their signatures, their docstrings, their call graph neighborhood, and the full source of the top matches — in 5–30 ms per query once warm.

Built for Python v0.1. Architecture is language-agnostic.

---

## Why this exists

An agent investigating an unfamiliar codebase burns tokens the same way every time:

1. `Grep` — 50–500 hits, most irrelevant.
2. `Read` three candidate files — 10–30 k tokens of which 95 % is noise.
3. `Grep` again for a name the agent just saw.
4. `Read` two more files to chase imports.
5. Synthesize, sometimes with gaps.

On a real 1,500-symbol codebase this takes **15–25 tool calls**, **30–80 k agent tokens**, and **60–120 seconds**.

`neargrep` does the same thing in **1 tool call**, **~15 k tokens**, **under 30 seconds cold / under 10 ms warm** — by doing the search → graph-walk → source-read once, on a pre-built index, and returning a single structured payload the agent can reason about immediately.

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

**First move for any code-understanding question: `mcp__neargrep__context`.** Pass a natural-language query or identifier. Returns top symbols with source, callees, callers, and file outlines — typically enough to answer without follow-up.

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

1. **`symbols` table** — every function, method, class, nested closure, and module-level or class-level constant. Fields: qualified name (`module.path:Class.method`), kind, signature, docstring, file, line range, decorators, base classes, source SHA.
2. **`calls` table** — caller → callee edges. Callee names are heuristically resolved against the caller's import table, with optimistic resolution through base-class MRO for `self.X` method calls. After full ingest, a demotion pass nulls out any callee qname that didn't land on a real symbol (so the agent doesn't chase dead `Model.objects.filter`-style chains).
3. **`symbols_fts`** (SQLite FTS5 virtual table) + **`symbol_vectors`** (384-dim `bge-small-en-v1.5` embeddings) — for search.

Incremental: files whose SHA matches the stored value are skipped. Only changed files get re-parsed and re-embedded.

**Cost on a real repo (biblereader backend, 278 files, 1,796 symbols):**
- Full index from scratch: ~20 seconds (embeddings dominate — pure parse+SQL is ~1.3 s)
- Re-index after editing one file: <500 ms

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
   - Up to `neighbor_limit=8` callees (signatures only).
   - Up to `neighbor_limit=8` callers (signatures only).
   - Full source body (capped at `body_char_cap=2000` chars each), for all top `source_for_top=5` seeds.
   - **Constant-alias resolution**: if the seed is `NAME = OTHER_NAME`, the chain is followed (up to 3 hops, cross-file) and the terminal literal is attached as `resolved_value`. So the agent sees the real string (`'claude-opus-4-5'`) without a separate `source` call on the registry module.
4. File outlines for up to 5 unique files among the seeds. Survey questions that span multiple files get multi-file context for free.

Typical output is 3–10 k tokens. Warm in-process latency is 5–8 ms.

### Ranking details & why hybrid won

We raced three modes on a real 1,600-symbol repo across two question types:

**Q1 — focused ("what fields does `TranslationVerse` have?"):**

| Mode | Calls | Tokens | Duration | Accurate? |
|---|---:|---:|---:|:---:|
| lexical | 4 | 23 k | 29 s | ✓ |
| hybrid | 4 | 21 k | 21 s | ✓ |
| **`context()`** | **1** | 24 k | **17 s** | **✓** |

**Q5 — survey ("list every LLM provider + model + phase"):**

| Mode | Calls | Tokens | Duration | Notes |
|---|---:|---:|---:|---|
| lexical (no constants) | 28 | 80 k | 112 s | missed `DEFAULT_*_MODEL` constants |
| lexical + constants | 16 | 54 k | 63 s | complete |
| vector-only | 6 | 44 k | 38 s | complete, some tail noise |
| composable hybrid | 15 | 41 k | 57 s | most thorough |
| **`context()`** | **1** | ~15 k | **~30 s cold / <5 s warm** | **complete with alias resolution** |

Highlights from the lexical-vs-vector head-to-head:

- *"TranslationVerse fields"* — lexical's top-3 were all **test methods** (BM25 camel-split matched `field_exists`). Vector got the real class. Hybrid with test-penalty matches vector here.
- *"which embedding model do we use"* — lexical matched on `we_process_batch` (wrong). Vector got `EmbeddingProvider`. Hybrid got the right answer.
- *"anthropic claude translation"* — all three worked. Hybrid was the most comprehensive (agent class + every DEFAULT_* constant).

**Why RRF, not score fusion?** FTS5 BM25 scores and cosine similarities live in different units. Normalizing is lossy and config-sensitive. RRF uses ranks alone and is robust — the only knob is the weight ratio, which we tuned once on real queries.

### Performance

All numbers measured on the biblereader backend (278 files, 1,796 symbols) on an M-series Mac.

| Path | Latency |
|---|---:|
| Cold CLI, exact qname (fast path) | 27 ms |
| Cold CLI, hybrid search | ~280 ms |
| Warm in-process, hybrid | **5–8 ms** |
| Warm in-process, exact qname | **1.2 ms** |

The cold-to-warm delta is almost entirely the fastembed ONNX model load. Inside an MCP server or long-running process, the model loads once and every subsequent query runs in single-digit ms.

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
        { "qname": "other:helper", "signature": "def helper(x)", "docstring": "…", "line": 55 }
      ],
      "callers": [ /* same shape */ ],
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
- Functions and methods (`def`, `async def`), including nested / closure definitions.
- Classes (with base-class list for MRO-aware `self.X` resolution).
- Module-level constants: `UPPER_CASE = literal | identifier | collection`.
- Class-level constants: `class C: STATUS_CHOICES = [...]`.
- Imports (for call-site resolution).
- Call edges, with optimistic `self.X` resolution through base classes.

**Not indexed (yet):**
- Strings inside source files (so `urlpatterns = [...]` route strings are not searchable).
- Docstrings of module files (only symbol docstrings).
- Comment blocks.
- Runtime-dynamic symbols (metaclasses, `type(...)` factories, monkeypatched attributes).

**Known rough edges:**
- `self.x.y.z` attribute chains are left unresolved by design — guessing is worse than saying "don't chase this".
- Django ORM-style `Model.objects.filter(...)` is demoted to unresolved after the post-ingest sweep (the chain doesn't point at real symbols).
- Class hierarchies where the base lives behind a runtime import or dynamic `__init_subclass__` will miss MRO edges.

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
      registry.py       # extension → parser dispatch
    walker.py           # gitignore-aware file walker
    index.py            # SQLite schema, FTS5, vector BLOB, incremental upsert
    embeddings.py       # fastembed + bge-small-en-v1.5
    api.py              # search_code, expand, outline, get_source, context, index_root
    cli.py              # argparse entry point
  tests/
    fixtures/sample_pkg/
    test_parser.py test_qname.py test_api.py test_constants.py
    test_demotion.py test_embeddings.py test_context.py test_context_alias.py
```

Add a new language by implementing `parsers/base.Parser` for its extension and registering it in `parsers/registry`. The SQLite schema, ranking, and `context()` logic are all language-agnostic.

---

## Running the test suite

```bash
# from the repo root
uv pip install --group dev      # or: uv pip install pytest
pytest
```

39 tests currently pass. First run downloads the ONNX embedding model (~30 MB, cached under `~/.cache/huggingface/`).

---

## Status and roadmap

**v0.1 core — complete and validated on a real 1,600-symbol codebase:**
- [x] Python AST extraction (functions, methods, classes, nested scopes, constants)
- [x] Call graph with MRO-aware `self.X` resolution
- [x] SQLite + FTS5 lexical search
- [x] `bge-small-en-v1.5` embeddings + cosine vector search
- [x] Weighted RRF hybrid ranker with test-file demotion
- [x] One-shot `context()` op with constant-alias resolution and multi-file outlines
- [x] Fast path for exact-qname queries (~1 ms warm)
- [x] Incremental indexing (SHA-based)

**Shipped:**
- [x] **MCP stdio adapter** (`neargrep-mcp`) — exposes all five ops to Claude Code / Cursor / Cline via `.mcp.json`.

**Planned next:**
- [ ] **Google ADK adapter** — thin `FunctionTool` wrappers over `neargrep.api` for in-process ADK agents.
- [ ] **TypeScript / TSX parser** — the parser layer is already plugin-shaped; add `parsers/typescript.py` using tree-sitter.
- [ ] **File watcher** — auto re-index on save.
- [ ] **`neargrep serve` daemon** — holds model + DB warm so even cold-CLI calls are single-digit ms.

---

## License

MIT.
