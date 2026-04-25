# snapctx

[![CI](https://github.com/snaptoolkit/snapctx/actions/workflows/ci.yml/badge.svg)](https://github.com/snaptoolkit/snapctx/actions/workflows/ci.yml)
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

`snapctx` answers the same question with **1 CLI call** returning **2–5 k tokens** of structured context. The call takes ~400 ms from a cold CLI (the embedding model loads once) — by doing the search → graph-walk → source-read → constant-alias resolution once, on a pre-built index, and returning a single structured payload the agent can reason about immediately. The agent's own reasoning time on top of that is still seconds, but the tool-call cost it pays drops by ~10× on calls and ~10× on tokens.

---

## Install

`snapctx` is a CLI tool. Install it globally with `uv tool` (isolated venv, entry points on `PATH`):

```bash
cd snapctx
uv tool install --editable .
```

That puts `snapctx` on your `PATH` (typically `~/.local/bin`).

Uninstall: `uv tool uninstall snapctx`.

### Requirements

- Python ≥ 3.11
- [`uv`](https://github.com/astral-sh/uv) for install (or use `pip install -e .` inside a venv)
- ~200 MB disk for the ONNX embedding model (downloaded on first index)

---

## 60-second quickstart

```bash
# Just ask a question. snapctx auto-indexes the repo on first use.
cd /path/to/your/repo
snapctx context "how does session authentication work"
```

The first query in a fresh repo builds the index transparently (one-time cost — typically 5–10 s for a few hundred files; subsequent queries are ~400 ms cold, ~5 ms inside `snapctx watch`). To pre-build explicitly:

```bash
snapctx index /path/to/your/repo
```

Output is JSON: top-5 matching symbols with full source for each, their callees and callers, and file outlines for the files involved. Usually enough to answer a non-trivial question without any follow-up.

For finer-grained operations (you'll rarely need them):

```bash
snapctx search  "rate limit"
snapctx outline src/auth.py
snapctx expand  "auth:login"  --direction callers   # who calls this?
snapctx source  "auth:login"  --with-neighbors      # body + callee signatures
```

### Auto-discovery: run from anywhere

`snapctx` walks up from the current directory to find the nearest `.snapctx/index.db`, so you don't need to pass `--root` every time. From a deeply nested file, queries still hit the right index.

### Multi-root: monorepos with separately indexed sub-projects

If you've indexed several sub-projects under one parent (typical monorepo: a Python `backend/` and a Next.js `frontend/`), launch `snapctx` from the parent and queries fan out across all of them:

```bash
# Each sub-project has its own .snapctx/
snapctx index ./backend
snapctx index ./frontend

# From the parent — no .snapctx of its own — queries hit both:
snapctx context "session login flow"
```

Each result is tagged with a `root` field (`"backend"` / `"frontend"`) so the agent knows which sub-project a symbol lives in. `expand` and `source` route to whichever root holds the qname; `outline` routes by file path prefix.

Use `snapctx roots` to see which indexes would be queried from your current directory.

---

## Use it from an AI agent

snapctx is a CLI tool. Any agent that can run shell commands (Claude Code, Cursor's terminal mode, custom Agent SDK loops, etc.) can use it via `Bash` calls. Each invocation pays a ~400 ms cold-start (model load); for tight loops, run `snapctx watch` in another tab and the index stays fresh as you edit.

### Tell the agent to reach for it first

Agents default to `Grep` + `Read` out of habit. Paste this into your project's `CLAUDE.md` / `AGENTS.md`:

```markdown
## Code exploration

This repo is indexed by `snapctx`. When you need to understand unfamiliar code, prefer it over `Grep`/`Read`:

**First move for any code-understanding question:** `snapctx context "<query>"`. Pass a natural-language query or identifier. Returns top symbols with source, a depth-2 call-path trace (callees-of-callees + callers-of-callers), and file outlines — typically enough to answer without follow-up.

**Drill-down** (only when `context` wasn't enough):
- `snapctx search "<query>"` — ranked symbols.
- `snapctx expand <qname> --direction callers|callees` — call-graph neighborhood.
- `snapctx outline <path>` — file symbol tree.
- `snapctx source <qname> --with-neighbors` — full body of one symbol.

snapctx walks up to find the right `.snapctx/index.db` automatically — no need to pass `--root`. From a parent containing several indexed sub-projects (monorepo with `backend/` + `frontend/`), queries fan out across all of them; each result is tagged with a `root` field telling you which sub-project it came from.

Fall back to `Grep` only for: URL route strings, TODO comments, filename patterns. snapctx indexes symbols, not raw text.

If you get a "no snapctx index found" error: run `snapctx index <repo-root>` first.
```

---

## Security model

snapctx is a local read-mostly CLI. Worth understanding what it touches.

**What it reads:**
- Files under the discovered `.snapctx` root, and only files the walker *indexed*:
  - Python (`.py`, `.pyi`) and TypeScript (`.ts`, `.tsx`, `.js`, `.jsx`) source — **not** `.env`, credentials, logs, binaries.
  - Respects `.gitignore` — anything excluded there is invisible.
  - Skips `.git`, `.venv`, `node_modules`, `__pycache__`, `.snapctx`, `dist`, `build`, `.tox` by default.

**What it writes:**
- Exactly one place: `<root>/.snapctx/index.db` (+ WAL/SHM sidecars). Nothing else on disk is ever touched.

**Network:**
- None at runtime. The ONNX embedding model is downloaded once on first `snapctx index` (into `~/.cache/huggingface/`), then every future query runs fully offline.

**Subprocess / code execution:**
- None. snapctx doesn't shell out, doesn't `eval`, doesn't spawn Python subprocesses. It parses files with the stdlib `ast` module + tree-sitter, runs SQLite queries, and does ONNX inference.

**Path-traversal protection:**
- `outline(path=…)` only returns symbols that exist in the index. A request for `/etc/passwd` or any other off-root path returns an empty result; no filesystem read happens.
- `source <qname>` reads the file path stored on the matched symbol row, which the walker guarantees is under the indexed root.

**The one real caveat — same as `Read` / `Grep`:**
Any secret hardcoded into a source file (API keys in module constants, credentials baked into source) becomes discoverable via the semantic search. `Read` and `Grep` already expose that; snapctx just makes it *more* findable. Don't commit secrets. If legacy code has them, add the file to `.gitignore`.

**Summary:** exposure = same set of files your agent could already `Read`/`Grep`, minus all non-source content, minus anything in `.gitignore`. No network, no exec, no writes outside `.snapctx/`.

---

## How it works

### Indexing (one-time per repo, re-runs incrementally)

`snapctx index <root>` walks the repo respecting `.gitignore` and per-extension parsers, then builds three artifacts in `<root>/.snapctx/`:

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

Caveat: all blind-eval agents made one-off CLI calls (each paying ~400 ms cold-start). The agent's own reasoning time (generating the follow-up calls it *didn't* need to make, writing the answer) dominates wall-clock, so the win for `context()` comes primarily from replacing **15 reasoning-and-call round-trips with 1**, not from making a single call faster.

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

The cold-to-warm delta is almost entirely the fastembed ONNX model load. Inside `snapctx watch` or any long-running process, the model loads once and every subsequent query runs in single-digit ms.

Indexing is I/O- and embedding-bound. The three levers we tuned (on the way from 85 s → 10 s on the same repo): walker-level vendor-bundle filter, TS signature truncation to 240 chars so massive `const X: ColumnDef<T>[] = [...]` declarations don't bloat the index, and `fastembed` batch size = 4 (counter-intuitive, but smaller batches mean less ONNX padding waste on mixed-length texts).

---

## The full API

Everything the CLI exposes is also available as a Python library. Import from `snapctx.api`:

```python
from snapctx.api import context, search_code, expand, outline, get_source, index_root

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

For monorepos with separately indexed sub-projects, the multi-root variants — `context_multi`, `search_code_multi`, `expand_multi`, `outline_multi`, `get_source_multi` — accept a list of roots and merge / route results across them. The CLI uses these automatically when `discover_roots()` returns more than one root.

```python
from snapctx.api import context_multi
from snapctx.roots import discover_roots

roots = discover_roots(".")            # walks up first; falls back to one-level walk-down
pack = context_multi("login session", roots, anchor=Path("."))
# Each seed in pack["seeds"] has a "root" field tagging which sub-project it came from.
```

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
snapctx/
  pyproject.toml
  src/snapctx/
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
                        #   + multi-root variants (search_code_multi, context_multi, ...)
    roots.py            # auto-discovery: walk-up to nearest .snapctx, walk-down for sub-projects
    cli.py              # argparse entry point (index, search, expand, outline,
                        #                       source, context, watch, roots)
  tests/
    fixtures/sample_pkg/
    test_parser.py  test_qname.py  test_api.py  test_constants.py
    test_demotion.py  test_embeddings.py  test_context.py  test_context_alias.py
    test_incremental.py  test_typescript_parser.py
    test_roots.py  test_multi_root.py  test_cli_discovery.py  test_watch.py
```

Add a new language by implementing `parsers/base.Parser` for its extension and registering it in `parsers/registry`. The SQLite schema, ranking, `context()` logic, and post-ingest promote/demote passes are all language-agnostic — the TypeScript parser slotted in without any changes outside its own file.

---

## Running the test suite

```bash
# from the repo root
uv pip install --group dev      # or: uv pip install pytest
pytest
```

100+ tests currently pass. First run downloads the ONNX embedding model (~30 MB, cached under `~/.cache/huggingface/`).

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
- [x] **Auto-discovery** — walks up from CWD to find the nearest `.snapctx/index.db`; falls back to one-level walk-down for monorepo parents with multiple indexed sub-projects
- [x] **Auto-indexing on first query** — if no index is reachable, `snapctx context|search|expand|outline|source` builds one transparently before answering
- [x] **Multi-root fan-out** — queries from a parent dir hit every indexed sub-project in parallel and tag results with their `root` label
- [x] **File watcher** (`snapctx watch`) — debounced auto re-index on save, typical run ~5 ms warm

**Planned next:**
- [ ] **TS scope tracker** — parameter / local / import resolution so TS callee traces aren't stuck at depth 1.
- [ ] **`snapctx serve` daemon** — holds model + DB warm so even cold-CLI calls are single-digit ms.

---

## License

MIT.
