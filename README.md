# snapctx

[![CI](https://github.com/snaptoolkit/snapctx/actions/workflows/ci.yml/badge.svg)](https://github.com/snaptoolkit/snapctx/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python: 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

*snapctx = **snap** **c**on**t**e**x**t — a snapshot of the context an agent needs.*

**Structured codebase context for AI agents.** One CLI call replaces the agent's usual `grep` + `read` + chase-imports loop. Ask a natural-language question; get back a self-contained pack of top symbols, their source, callees, callers, and module-level docstrings. **6× fewer tool calls. 8× faster. 2× fewer tokens on survey queries. Up to 16× fewer calls in realistic agent usage.** Measured against minimum-grep on this codebase — [see the full numbers](#tool-benchmark).

Languages today: Python, TypeScript, TSX, JSX, and shell (`.sh`/`.bash`). The parser layer is pluggable — adding a language is a single new file.

---

## Get started in 60 seconds

```bash
# 1. Clone and install (one time). Puts `snapctx` on your $PATH.
git clone https://github.com/snaptoolkit/snapctx.git
cd snapctx
uv tool install --editable .

# 2. Go to any code repo and ask a question. snapctx auto-builds the index on first use.
cd /path/to/your/repo
snapctx context "how does session authentication work"
```

That's it. You get JSON back: top-5 matching symbols, their full source, who they call, who calls them, file outlines for the surrounding files. Usually enough to answer a non-trivial question in one call.

Here's real output from running that query against the `requests` library (~370 ms cold CLI; under 10 ms with `snapctx watch` running):

```jsonc
{
  "query": "how does session authentication work",
  "mode": "hybrid",
  "seeds": [
    {
      "rank": 1,
      "qname": "requests.sessions:SessionRedirectMixin.rebuild_auth",
      "kind": "method",
      "signature": "def rebuild_auth(self, prepared_request, response)",
      "docstring": "When being redirected we may want to strip authentication from the request to avoid leaking credentials.",
      "file": "requests/sessions.py",
      "lines": "282-300",
      "score": 0.0479,
      "callees": [
        {
          "qname": "requests.sessions:SessionRedirectMixin.should_strip_auth",
          "signature": "def should_strip_auth(self, old_url, new_url)",
          "docstring": "Decide whether Authorization header should be removed when redirecting",
          "line": 290,
          "callees": [                              // depth-2 hop
            { "qname": "?:urlparse", "line": 130, "resolved": false }
          ]
        }
      ],
      "callers": [
        {
          "qname": "requests.sessions:SessionRedirectMixin.resolve_redirects",
          "signature": "def resolve_redirects(self, resp, req, …)",
          "docstring": "Receives a Response. Returns a generator of Responses or Requests.",
          "line": 246,
          "callers": [                              // depth-2 hop
            { "qname": "requests.sessions:Session.send", "line": 725 }
          ]
        }
      ],
      "source": "def rebuild_auth(self, prepared_request, response):\n    …"
    }
    // … 4 more seeds
  ],
  "file_outlines": [
    {
      "file": "requests/sessions.py",
      "symbols": [
        { "qname": "requests.sessions:merge_setting",   "kind": "function", "lines": "62-89"   },
        { "qname": "requests.sessions:SessionRedirectMixin", "kind": "class", "lines": "107-353" },
        { "qname": "requests.sessions:Session",         "kind": "class",    "lines": "354-833"  }
        // …
      ]
    }
  ],
  "token_estimate": 5552,
  "hint": "This response bundles search + callees + callers + top sources + a file outline. If it's still not enough, call `expand`, `outline`, or `source` on a specific qname."
}
```

**Requirements:** Python ≥ 3.11, [`uv`](https://github.com/astral-sh/uv) (or `pip install -e .` in a venv), ~200 MB disk for the ONNX embedding model (downloaded on first index).

**Uninstall:** `uv tool uninstall snapctx`.

---

## Tool benchmark

Controlled minimum-grep vs warm snapctx Python API (model pre-loaded, as with `snapctx watch`). Tokens estimated at 4 chars/token from actual bytes returned by tools — no agent reasoning included.

| Query | Type | Calls (grep → snapctx) | Speed | Tokens (grep → snapctx) | Token content |
|---|---|---|---|---|---|
| Search pipeline end-to-end | survey | 6 → 1 (**6× fewer**) | 34 ms → 6 ms (**6× faster**) | 13.8 k → 6.5 k (**2× fewer**) | grep reads whole files; snapctx returns filtered symbol bodies |
| Every SQLite connection open | audit | 4 → 2 (**2× fewer**) | 51 ms → 6 ms (**8× faster**) | 1.1 k → 2.4 k (2.2× more) | grep returns raw matched lines; snapctx returns qname + file + call-graph per hit |
| Multi-root discovery logic | architecture | 3 → 1 (**3× fewer**) | 16 ms → 5 ms (**3× faster**) | 4.2 k → 6.6 k (1.6× more) | grep reads two full files; snapctx includes call-graph depth and neighbors grep cannot produce |

Audit and architecture queries return more tokens from snapctx, but the content is structured — every hit already carries its enclosing `qname`, call-graph neighbors, and file outline. The agent synthesises directly without further reads. These are minimum grep counts; real agents make more exploratory calls, widening the call-count gap to 16×.

---

## The commands

You'll mostly use **`context`**. The others let you drill in when `context` isn't enough; **`find`** is the right tool when you need exhaustive grep-style coverage instead of ranked top-K.

| Command | What it does | When to use |
|---|---|---|
| `snapctx context "query"` | Everything-in-one-call: search + callees + callers + source + outlines. **Audit-aware**: when the query is an unambiguous audit phrasing (e.g. "audit every `transaction.atomic` site"), also runs `find` on the literal and attaches an exhaustive `find_results` block. | First move for any question. ~3–10 k tokens back. |
| `snapctx search "query"` | Top-K ranked symbols with signatures. Add `--with-bodies` to inline source. Add `--also <term2> [...]` to batch related terms in one call. | Ranked discovery; `--with-bodies` for one-shot audits when ≤ K hits. |
| `snapctx find "<literal>"` | **Exhaustive** literal-substring enumeration over every indexed symbol body. Returns ALL matches (not ranked, not capped). Add `--with-bodies` to inline containing-symbol source; add `--with-callers` to attach depth-1 callers (deduped) to every hit. | "Every place that uses X" audits — matches grep coverage with structured output. `--with-callers` turns it into "every site AND who triggers them" in one call. |
| `snapctx map [--prefix PATH] [--depth 1\|2]` | Repo-wide table of contents — every indexed file's top-level symbols (signature, 1-line docstring, decorators), grouped by directory. No query needed. `--depth 2` also pulls in direct children (class methods). `--prefix` scopes to a sub-tree (e.g. `src/`). | Orientation when you don't yet have a specific question — fresh repo, unfamiliar area. Pairs with `search` for the actual lookup once you've oriented. ~6–30 k tokens depending on scope. |
| `snapctx outline path/` | Symbol tree of a file or directory (functions / classes / constants, nested). Add `--with-bodies` to inline source for every symbol. | Cheaper than reading whole files; directory mode gives you a module map. |
| `snapctx source <qname>` | Full body of a single symbol. Add `--with-neighbors` for resolved callee signatures. | When you have an exact qname and want its source. |
| `snapctx expand <qname>` | Walk the call graph. `--direction callees \| callers \| both`, `--depth 1 \| 2`. | "Who calls this?" / "What does this depend on?" |
| `snapctx edit <qname> <body_file>` | **Write op**. Replace a symbol's body by qname (re-indexes the file before returning). `--stdin` reads the body from stdin instead of a file. Refuses if the file's SHA has drifted since indexing — the caller must re-query for fresh coordinates. **Syntax pre-flight**: refuses Python (`ast.parse`) or TS/TSX (tree-sitter) edits that would leave the file unparseable. | Editing a function or class without re-reading the whole file. Pair with `source` (to see what's there) → write the replacement → `edit`. |
| `snapctx insert <anchor_qname> <body_file> [--position before\|after]` | **Write op**. Insert a NEW top-level symbol adjacent to an anchor symbol. Same staleness + syntax guards as `edit`. | Adding a new function or class without rewriting the whole file. |
| Python API: `edit_symbol_batch(edits, root)` | **Write op (Python only — no CLI yet)**. Apply N edits in one call, grouped by file, per-file atomic. A syntax error in any edit on a file rolls back THAT file; other files in the batch land independently. One re-index for the whole batch. | Multi-symbol refactors (rename across N callers, add validation to N functions). |
| Python API: `delete_symbol(qname, root)` | Remove a symbol entirely. Drops the line range and trims one leading blank line so PEP-8 spacing between top-level fns survives the deletion. | Clean removal — `edit_symbol(q, "")` would corrupt surrounding spacing. |
| Python API: `add_import(file, statement, root)` / `remove_import(file, statement, root)` | Add or remove an import line. Idempotent (`already_present` / `already_absent` in the response). Imports live above the first symbol and aren't reachable via `edit_symbol`; these fill that gap. | Updating imports after a rename, adding a new dependency, removing dead imports. |
| Python API: `create_file(path, content, root)` / `delete_file(path, root)` / `move_file(old, new, root)` | File-level write ops. `create_file` writes a new file + reindexes; `delete_file` unlinks + drops symbols from the index; `move_file` renames + reindexes and returns `importing_files` so the caller can rewrite imports via `add_import` / `remove_import`. | Adding / removing / moving whole modules. |

The query can be:
- **Natural language**: `"how does rate limiting work"`, `"where do we verify credentials"`
- **An identifier**: `"SessionManager"`, `"verify_credentials"`
- **An exact qname** (fastest path, ~30 ms): `"app.auth:SessionManager.refresh"`

Add `--kind function|method|class|component|constant|interface|type` to any of these to narrow results.

---

## What just works (no setup)

- **Auto-indexing on first query.** Run `snapctx context …` in a fresh repo and it builds the index transparently before answering.
- **Forgiving qnames.** `source` and `expand` tolerate common LLM paraphrases — keep `.tsx`/`.py` on the module (`components/Verse.tsx:Verse`), apply Python dotted style to a TS file (`components.Verse:Verse`), or vice versa — and the call still resolves. The response includes a `paraphrase_hint` field naming the canonical form so the caller learns it for next time.
- **Auto-refresh on every query.** Subsequent runs incrementally re-index files whose SHA changed — usually <200 ms.
- **No `--root` flag.** snapctx walks up from your current directory to find the nearest `.snapctx/index.db`. Run from a deeply nested file; queries still hit the right index.
- **Stderr for progress, stdout for JSON.** Pipe stdout to `jq` without it choking on log lines.

To pre-build explicitly (one-time, ~5–10 s for a few hundred files):

```bash
snapctx index /path/to/your/repo
```

---

## Use it from your AI agent

snapctx is a CLI tool. Any agent that can run shell commands (Claude Code, Cursor's terminal, custom Agent SDK loops) can use it via `Bash` calls. Agents tend to default to `Grep` + `Read` out of habit — paste this into your project's `CLAUDE.md` / `AGENTS.md` to redirect them:

```markdown
## Code exploration with snapctx

For ANY question about unfamiliar code in this repo, your first move is `snapctx context "<query>"`. It uses fewer tokens, fewer tool calls (1 vs 11–50), and guarantees 100% coverage on exhaustive audits where `Grep` + `Read` chains miss the long tail.

**Why it works**: snapctx parses the codebase into a symbol graph (functions, classes, components, constants, calls, imports), runs hybrid lexical+semantic search, and returns top symbols with source bodies, a depth-2 call-path trace (callees-of-callees + callers-of-callers), constant-alias resolution, and file outlines — usually enough to answer in one call.

### First move

`snapctx context "<query>"` — query can be natural language, an identifier, or an exact qname (`app.auth:SessionManager.refresh`).

Returns JSON with `seeds[]` (ranked symbols + bodies + neighbors), `file_outlines[]`, and a `hint`. Typical response 3–10 k tokens.

**Audit-aware**: phrasings like *"audit every transaction.atomic site"*, *"list every useState call"*, *"every place that uses X"* trigger a parallel exhaustive `find` on the literal — the response gets a `find_results` block listing every site (file:line, qname, matching line). Saves you from having to know whether to use `context` or `find` for cross-cutting audit questions.

### Drill-down (only if `context` wasn't enough)

- `snapctx search "<query>"` — top-K ranked symbols. Add `--with-bodies` to inline source; add `--also <term2> [...]` to batch related terms.
- `snapctx find "<literal>" --with-bodies` — **exhaustive** substring enumeration across all indexed bodies. Use this for "every place that uses X" audits where ranked search would cap the long tail. Add `--with-callers` to also attach the deduped depth-1 caller list to each hit (audit + impact analysis in one call).
- `snapctx map --prefix src/` — query-free *orientation* call. Returns the whole code tree (every file's top-level symbols + signatures + 1-line docstrings + decorators, grouped by directory) so you can build a mental model before you have a specific question. `--depth 2` adds class methods. Use this when landing in an unfamiliar repo or area; for actual lookups, follow with `search` or `context`.
- `snapctx expand <qname> --direction callees|callers|both --depth 1|2` — call-graph neighborhood.
- `snapctx outline <path>` — symbol tree of a single file or directory; add `--with-bodies` to inline every symbol's source.
- `snapctx source <qname> --with-neighbors` — full body + resolved callee signatures.

### What just works

- **No setup**: queries auto-index on first use; auto-refresh on subsequent runs picks up your edits.
- **No `--root`**: snapctx walks up from CWD to find the nearest `.snapctx/index.db`.
- **Monorepos**: launching from a parent of indexed sub-projects fans out queries; each result has a `root` field (`"backend"` / `"frontend"`).
- **Third-party code on demand**: prefix a query with a package name (`"django: queryset filter"`) to route to that package's own isolated index. First use ingests the package; subsequent calls are zero-cost. No prefix → repo only. `--pkg <name>` is the equivalent for ops without a free-text query (`source`, `outline`, `expand`).
- **Filters**: `--kind function|method|class|component|constant|interface|type` to narrow results.

### When to fall back to Grep

snapctx indexes **symbols**, not raw text. Use `Grep` for: URL routes (`"/api/v1/users"`), TODO/FIXME comments, env var names, filename patterns. For everything else (function bodies, class structure, "where is X used", "how does Y work") — use snapctx. For literal substrings that *are* code identifiers or call-site fragments (`"transaction.atomic"`, `"useState"`), prefer `snapctx find` over `Grep`: same exhaustive coverage, with the containing qname attached to every hit.

### Tips

- If `context` returns the wrong area, paraphrase the query — hybrid mode adapts ranker weights to query style.
- If you need just signatures (no bodies), use `search` instead of `context` to save tokens.
- `--mode lexical` skips the embedder (~50 ms cold) — use when the query is an exact identifier.
```

---

## Why bother (the numbers)

An agent investigating an unfamiliar codebase burns tokens the same way every time: grep for some terms, read 2–3 candidate files in full, grep again, read more files, synthesize. The cost compounds with question difficulty.

We measured both paths on three real codebases (a Django backend, zustand, and snapctx itself) — **9 cross-cutting questions** of the kind agents actually struggle with: *"audit every place that calls into an LLM provider"*, *"trace how a state update propagates from setState through equality checks to subscribers"*, *"list every CLI subcommand and which API function it dispatches to"*. snapctx ran via the in-process Python API (the path you get with `snapctx watch` or library use); grep+read used real `grep -ri` calls plus reading the top-K most-frequently-matching files (K = 3 for narrow, 6 for survey, 10 for audit; matching realistic agent behavior on each difficulty).

| Codebase | Difficulty | Question (abridged) | snapctx tokens | grep+read tokens | **ratio** | tool calls |
|---|---|---|---:|---:|---:|---:|
| Django backend | audit  | every Django model field + app | 7 k | 155 k | **22×** | 1 → 14 |
| Django backend | audit  | every LLM provider call + model + temperature | 10 k | **204 k** | **20×** | 1 → 17 |
| Django backend | survey | full flow API → views → DB for verse fetch | 14 k | 58 k | 4× | 1 → 11 |
| Django backend | survey | every DRF throttle / permission class + where | 7 k | 23 k | 3× | 1 → 11 |
| zustand | audit  | setState → equality → subscribers + persist | 6 k | 95 k | **17×** | 1 → 18 |
| zustand | survey | every middleware + state shape | 5 k | 77 k | **15×** | 1 → 11 |
| snapctx | audit  | every Index() open + connection lifecycle | 6 k | 40 k | 7× | 1 → 18 |
| snapctx | audit  | every CLI subcommand + API dispatch | 4 k | 44 k | **10×** | 1 → 17 |
| snapctx | survey | snapctx context → embedder load → SQLite search | 9 k | 41 k | 5× | 1 → 11 |

**The pattern.** Audit-class questions ("list every X and its Y") — where grep+read forces the agent to read 8–14 files in full — consistently hit **10–22× token reduction**. Survey questions ("trace this flow") land at **3–5×**. The harder the question, the wider the gap.

**Exhaustive literal audits — `find` vs ranked `search`.** Some audits are about *every* call site of a known string, not the most relevant ones. Ranked search caps the long tail; `find` walks the indexed bodies and returns every hit. Measured on a Django backend with a Sonnet sub-agent answering *"audit every `transaction.atomic` site"*:

| Approach | Sites found | snapctx calls | Bash calls | Tokens | Wall |
|---|---:|---:|---:|---:|---:|
| `search --mode lexical -k 100 --with-bodies` | 9 / 22 | 11 | 12 | 37 k | 185 s |
| `context "audit every transaction.atomic site"` (audit-aware) | 22 / 22 | 2 | 3 | 36 k | 81 s |
| **`find "transaction.atomic" --with-bodies`** | **22 / 22** | **1** | **1** | **33 k** | **61 s** |
| `find "..." --with-bodies --with-callers` | 22 / 22 | 1 | 2 | 35 k | 67 s |
| Reference: `grep -rn` + read top files | 22 / 22 | n/a | 50 | 39 k | 146 s |

`find` matches `grep` exactly (22 / 22) in a single tool call. The ranked-search variant misses ~60% because the agent stops after a few overlapping `-k 100` pages. **For audit-class questions, the bottleneck wasn't ranking quality — it was the contract: ranked + capped vs exhaustive.**

**`find` vs `grep -rn` — the measured gap:**

- **50× fewer tool calls** (1 vs 50). Every grep iteration forces the agent to read raw lines, infer which file to open next, issue a read, and decide whether to grep again. `find` does all of that in the index.
- **2.4× faster wall clock** (61 s vs 146 s).
- **Output tokens roughly equal** (33 k vs 39 k) — the token savings vs `grep` aren't the story here. The story is the call-count collapse and what the output *is*.

What `grep` returns that the agent still has to process: raw matched lines with no surrounding context, no function name, no caller information. What `find` returns: `qname` (the enclosing function or method, resolved), file, line, and the matching line — structured, ready to synthesize. Add `--with-bodies` and you get the full enclosing symbol; add `--with-callers` and you get impact analysis in the same call. There is no single `grep` command that does any of those.

The audit-aware `context` row is the win for agents that default to `context` as their first move: same coverage as raw `find` (22 / 22), at the cost of one extra targeted call to fetch bodies. They no longer need to know whether to reach for `context` or `find` — `context` routes them. The `--with-callers` row trades ~6 s wall and ~2 k tokens for inline impact analysis; in our run it produced a sharper synthesis that explicitly addressed orphaned atomic blocks.

**Latency for snapctx itself:**

- **Warm path** (in-process, with `snapctx watch` or library use, indexed): **6–16 ms per query** — the embedder model loads once and stays resident.
- **Cold path** (one-shot CLI invocation, indexed): **~0.3–1.1 s** (most of that is the ~250 ms fastembed model load each run).
- **First query in a fresh repo** (auto-indexes): ~5–10 s for a few hundred files. After that, every refresh is SHA-skip and finishes in <200 ms.

The cold-CLI ~1 s vs warm 6 ms isn't the win that matters at the agent level; the round-trip-count collapse is. But if your agent uses snapctx through `snapctx watch` or imports the Python API, every query lands under 20 ms.

---

## Monorepos

If you've indexed several sub-projects under one parent (typical: a Python `backend/` and a Next.js `frontend/`), launch `snapctx` from the parent and queries fan out across all of them:

```bash
snapctx index ./backend
snapctx index ./frontend

# From the parent — no .snapctx of its own — queries hit both:
snapctx context "session login flow"
```

Each result is tagged with a `root` field (`"backend"` / `"frontend"`). `expand` and `source` route to whichever root holds the qname; `outline` routes by file path prefix.

`snapctx roots` shows which indexes would be queried from your current directory.

---

## Querying third-party packages

A query can target a third-party package by **prefixing the package name**. No prefix → the repo's own code, full stop. Prefixed → snapctx ensures that one package is indexed (one-time, on demand) and runs the query against the package's *own* isolated index.

```bash
# Repo only — no vendor packages searched.
snapctx context "session login"

# Routes to django's per-package index. First call ingests just django
# (one-time ~50 s for ~900 files). Subsequent calls are zero-cost.
snapctx context "django: queryset filter chain lazy"
# → { "scope": "django", "seeds": [ { "qname": "db.models.query:QuerySet._chain", … } ] }
```

**Why per-package isolation rather than merging into the repo's index?** Two reasons. (1) Vector-search neighborhoods are sharper over a single coherent corpus — searching for `QuerySet` inside the django scope can't be polluted by your own filter classes. (2) Qnames inside the package's index are re-rooted at the package, so they look like Django's actual module structure (`db.models.query:QuerySet`) instead of a long `.venv.lib.pythonX.Y.site-packages.django.…` prefix.

**Storage layout:**

```
<root>/.snapctx/
  index.db                  ← repo (default — what queries hit with no prefix)
  vendor/
    django/index.db         ← one isolated index per indexed package
    react/index.db
```

**Routing rules** for the `<pkg>:` prefix:
- The head must be a single identifier (letters, digits, underscore, hyphen) — `django.db.models:QuerySet` stays a qname, not a prefix.
- The head must match a directory under `<root>/{.venv,venv,env}/lib/python*/site-packages/<name>/` (Python) or `<root>/node_modules/<name>/` (Node, top-level only). An already-indexed package also matches even if its source dir was later deleted.
- No match → no routing, the colon is treated as part of the query.

**Manual control** for ops that don't take a free-text query:

```bash
snapctx source --pkg django "db.models.query:QuerySet"  # equivalent of "django: …"
snapctx vendor list                                      # see indexed + available
snapctx vendor forget django                             # drop a package's index
```

`--pkg` is available on `search`, `context`, `expand`, `outline`, and `source`. Vendor scoping is single-root only — run from inside the specific sub-project that owns the venv.

**Subsequent scoped calls are fast.** A repo query auto-refreshes the repo's index (SHA-skip ~750 ms on a 300-file project). A scoped query *skips* the repo refresh entirely. End-to-end latency: **~350 ms warm**, dominated by fastembed model load.

### Cross-package call graph (lazy stitching)

When `expand` or `context` traverses a call inside one indexed package and the callee was imported from *another also-indexed* package, the resolver follows the file's `imports` table to the right sibling index and returns the resolved symbol with a `package` tag.

Example: inside django, `tasks.base:Task.call` invokes `async_to_sync`. If you've also indexed `asgiref:`, `expand` returns:

```jsonc
{ "qname": "sync:async_to_sync", "package": "asgiref", … }
```

instead of an unresolved name. Honors the explicit-prefix rule: cross-resolution *only* peeks into packages you've already chosen to index — it never spontaneously fans out into something you didn't ask for. Calls into unindexed packages stay marked `resolved: false`, and you can `--pkg <name>` to bring them in.

---

## Per-repo config (optional)

Drop a `snapctx.toml` at the repo root to override walker defaults — extra skip directories, vendor-bundle filter, file-size cap, language enable list, glob include/exclude. Without a config file, behavior is unchanged.

```toml
# snapctx.toml — every key optional. Defaults match the no-config behavior.

[walker]
# Add to the always-skip directory list (joined with .git, .venv,
# node_modules, vendor, dist, build, ...).
extra_skip_dirs = ["legacy", "third_party"]

# Add filename suffixes to the vendor-bundle skip list (joined with
# .min.js, .bundle.js, *-bundle.js, *.standalone.js, .map, ...).
extra_skip_suffixes = [".generated.ts"]

# Force-include paths even when .gitignore would skip them.
extra_include = ["vendor/internal-fork/**"]

# Force-exclude regardless of .gitignore.
extra_exclude = ["docs/generated/**", "**/*.snapshot.tsx"]

# Toggles (defaults match current behavior).
skip_vendor_bundles  = true      # filter .min.js / *-bundle.js / .map / ...
skip_vendor_packages = true      # filter node_modules / .venv / vendor / ...
respect_gitignore    = true      # honor .gitignore
max_file_size        = 256000    # bytes; default 250 KiB

# Restrict to specific parsers (default: every parser is active).
# Valid values: "python", "typescript", "shell".
languages = ["python", "typescript", "shell"]
```

Unknown keys are tolerated (forward-compat); type errors on known keys raise with the file path so they're easy to fix.

---

## Keep the index hot: `snapctx watch`

```bash
snapctx watch
```

Sits on the repo, debounces filesystem events, re-indexes on save (typically <200 ms per delta because of SHA-skip). Inside the watch process the embedder stays loaded, so query latency drops to single-digit ms.

---

## How it works

### Indexing (one-time per repo, re-runs incrementally)

`snapctx index <root>` walks the repo respecting `.gitignore` and per-extension parsers, then builds three artifacts in `<root>/.snapctx/`:

1. **`symbols`** — every function, method, class, nested closure, interface, type alias, React component, module (with file-level docstring or leading JSDoc), and module-level / class-level constant. Fields: qualified name (`module.path:Class.method`), kind, signature, docstring, file, line range, decorators, base classes, source SHA.
2. **`calls`** — caller → callee edges. Names are heuristically resolved against the caller's import table, with optimistic resolution through base-class MRO for `self.X` method calls. Calls in decorator arguments and default values are attributed to the module, not the decorated function. After full ingest, two post-passes fix up edges:
   - **Demote** — null any callee qname that didn't land on a real symbol.
   - **Promote** — resolve forward-referenced `self.X()` calls where the target was defined later in the same class body.
3. **`symbols_fts`** (SQLite FTS5) + **`symbol_vectors`** (384-dim `bge-small-en-v1.5` embeddings) — for hybrid search.

Incremental: files whose SHA matches the stored value are skipped. Only changed files get re-parsed and re-embedded.

The walker skips vendored/bundled assets by default: `node_modules`, `.venv`, `dist`, `vendor`, `bower_components`, `*.min.js`, `*.bundle.js`, `*-bundle.js`, `*.standalone.js`, `*.lib.js`, `*.worker.js`, `*.map`, plus any source file over 250 KB.

### Query-time

All operations read from the same SQLite file. They're cheap and composable; `context` is the all-in-one wrapper.

- **`search_code(query, k, kind?, mode, with_bodies?, also?)`** — three modes: `lexical` (FTS5/BM25, ~2 ms), `vector` (cosine over embeddings, ~5 ms), `hybrid` (weighted RRF of both, default). Hybrid uses `vec_weight=1.5`, `lex_weight=1.0`, plus a `test_penalty=0.6` multiplier so test methods don't out-rank real code. `with_bodies=True` inlines source bodies (with constants pre-resolved) for one-shot audits; `also=[…]` runs the same query for several related terms in a single call.
- **`find_literal(literal, kind?, in_path?, with_bodies?, with_callers?)`** — exhaustive literal-substring scan over indexed symbol bodies. Returns every match (file, qname, match_line, match_text), innermost-symbol deduped so a method beats its enclosing class. Complement to `search_code` when the question is "every place that uses X" rather than "the most relevant place". `with_callers=True` attaches the deduped depth-1 caller list to each hit so audit + impact analysis fits in one call.
- **`expand(qname, direction, depth)`** — walk the call graph. Returns signatures + docstring summaries of neighbors, no bodies.
- **`outline(path, with_bodies?)`** — file or directory symbol tree, nested by containment; `with_bodies=True` inlines source for every symbol.
- **`map_repo(prefix?, depth?)`** — repo-wide table of contents. Returns every indexed file's top-level symbols (qname, signature, 1-line docstring, line range, decorators), grouped by directory, with each file's module docstring hoisted to a file-level `summary`. `depth=2` adds direct children (class methods, nested functions). `prefix` scopes to a sub-tree. Query-free orientation tool — complement to `context`/`search`, not a replacement.
- **`get_source(qname, with_neighbors)`** — full source of a single symbol; `with_neighbors=True` appends signatures of resolved callees.
- **`context(query, …)`** — the one-shot. Runs `search_code` (or fast-paths to a direct qname match when the query contains `:` and matches a known qname). For each of the top `k_seeds=5` hits:
  - Signature, docstring, file, line range, decorators, score.
  - **Depth-2 call trace** — up to `neighbor_limit=8` direct callees, each resolved callee carrying up to `max(3, neighbor_limit//2)` of its own callees. Same shape for callers. An agent sees the full flow (e.g. `Runner.emit → _publish_delta → client.publish`) without a follow-up `expand` call. Unresolved calls to Python builtins (`print`, `len`, `isinstance`, …) are filtered so the graph stays focused on domain code.
  - Full source body (capped at `body_char_cap=2000` chars each) for the top `source_for_top=5` seeds.
  - **Constant-alias resolution**: if the seed is `NAME = OTHER_NAME`, the chain is followed (up to 3 hops, cross-file) and the terminal literal is attached as `resolved_value`. So the agent sees the real string (`'claude-opus-4-5'`) without a separate `source` call on the registry module.

  File outlines for up to 8 unique files among the search candidates — the candidate pool is overfetched beyond the top-K seeds (default `outline_discovery_k=15`) so survey questions get full coverage. Typical output: 3–8 k tokens.

  **Audit-aware enrichment.** A conservative classifier (`extract_audit_literal`) detects audit phrasings that wrap a single literal — *"audit every X"*, *"every place that uses X"*, *"list every X"* — and returns the literal only when exactly one identifier-shaped token survives stripping audit fillers ("site", "call", "place", "uses", …). On a hit, `context` runs `find_literal` in the same call and attaches a `find_results` block (file, qname, match_line, match_text per site, no bodies — agent re-issues `find <lit> --with-bodies` if needed). Multi-literal questions ("every LLM provider call") and concept questions ("every model field") deliberately skip the find block to avoid clutter.

### Why hybrid won

We raced lexical / vector / hybrid on real codebases:

**Q1 — focused** (*"what fields does `TranslationVerse` have?"*):

| Mode | Calls | Tokens | Duration | Accurate? |
|---|---:|---:|---:|:---:|
| lexical | 4 | 23 k | 29 s | ✓ |
| hybrid | 4 | 21 k | 21 s | ✓ |
| **`context()`** | **1** | 24 k | **17 s** | **✓** |

**Q5 — survey** (*"list every LLM provider + model + phase"*):

| Mode | Tool calls | Agent tokens | Duration | Notes |
|---|---:|---:|---:|---|
| lexical (no constants) | 28 | 80 k | 112 s | missed `DEFAULT_*_MODEL` constants |
| lexical + constants | 16 | 54 k | 63 s | complete |
| vector-only | 6 | 44 k | 38 s | complete, some tail noise |
| composable hybrid | 15 | 41 k | 57 s | most thorough |
| **`context()`** | **1** | 42 k | **30 s** | **complete with alias resolution** |

The win for `context()` comes from collapsing 15 reasoning-and-call round-trips into 1, not from making a single call faster.

The RRF math is just `rrf(q) = Σ weight / (60 + rank)` — sum the weighted reciprocal-rank contributions from each ranker. The 60 is the standard RRF constant; the only tunable is the weight ratio.

**Why RRF, not score fusion?** FTS5 BM25 and cosine similarity live in different units; normalizing is lossy and config-sensitive. RRF uses ranks alone — robust, and the only knob (the weight ratio) was tuned once on real queries.

### Performance

Measured on a mixed-language monorepo (Python Django backend + Next.js frontend, 754 files, 3,290 symbols), M-series Mac:

| Path | Latency |
|---|---:|
| Cold index from scratch (parse + embed all symbols) | ~10 s |
| Re-index after one-file edit (SHA-skip the rest) | <200 ms |
| Cold CLI, hybrid `context()` call | ~380 ms |
| Cold CLI, exact qname (fast path) | ~30 ms |
| Warm in-process, hybrid `context()` (depth-2 trace) | **5–10 ms** |
| Warm in-process, exact qname | **1–2 ms** |

The cold-to-warm delta is almost entirely the fastembed ONNX model load. Inside `snapctx watch` (or the internal serve daemon — `python -m snapctx._serve`, used by the warm client) the model loads once and every query is single-digit ms. A first-class `snapctx serve` CLI command is on the roadmap.

The three levers we tuned (85 s → 10 s on the same repo): walker-level vendor-bundle filter, TS signature truncation to 240 chars so massive `const X: ColumnDef<T>[] = [...]` declarations don't bloat the index, and `fastembed` batch size = 4 (counter-intuitive, but smaller batches mean less ONNX padding waste on mixed-length texts).

---

## Use it as a Python library

Everything the CLI exposes is also a Python function. Import from `snapctx.api`:

```python
from snapctx.api import (
    context, search_code, find_literal, expand, outline, map_repo,
    get_source, index_root,
    # write ops:
    edit_symbol, insert_symbol, edit_symbol_batch, delete_symbol,
    add_import, remove_import,
    create_file, delete_file, move_file,
)

# Build or refresh the index.
index_root("/path/to/repo")

# One-shot context pack.
pack = context("how does session authentication work", root="/path/to/repo")
for seed in pack["seeds"]:
    print(seed["qname"], seed["signature"])
    if "resolved_value" in seed:
        print("  → ", seed["resolved_value"]["value"])

# Exhaustive literal-substring audit.
result = find_literal("transaction.atomic", root="/path/to/repo", with_bodies=True, with_callers=True)
for match in result["matches"]:
    print(match["qname"], match["file"], match["match_line"])
    for caller in match.get("callers", []):
        print("  ←", caller["qname"])

# Composable operations.
hits = search_code("throttle", k=3, mode="vector", root="/path/to/repo")
neighbors = expand("auth.service:login", direction="callers", root="/path/to/repo")
tree = outline("src/auth/service.py", root="/path/to/repo")
src = get_source("auth.service:login", with_neighbors=True, root="/path/to/repo")
```

All functions return JSON-serializable dicts.

### Write operations

The same qname-based addressing also drives writes. `edit_symbol` replaces a single symbol's body, `insert_symbol` adds a new one next to an anchor, `edit_symbol_batch` applies many edits in one call:

```python
from snapctx.api import edit_symbol, insert_symbol, edit_symbol_batch

# Replace one symbol's body. Splices in at the indexed line range,
# checks the file's SHA hasn't drifted since indexing, runs a syntax
# pre-flight (ast.parse for Python, tree-sitter for TS/TSX), and
# re-indexes the file before returning.
edit_symbol(
    "auth.service:login",
    "def login(username, password):\n    return _verify(username, password)\n",
    root="/path/to/repo",
)

# Add a brand-new top-level function next to an anchor.
insert_symbol(
    "auth.service:login",
    "\n\ndef logout(session_id):\n    return _drop(session_id)\n",
    root="/path/to/repo",
    position="after",
)

# Apply many edits in one call. Per-file atomic: a syntax error on
# any edit in file X rolls back X's edits; files Y, Z still land.
# One re-index for the whole batch.
edit_symbol_batch(
    [
        {"qname": "auth.service:login",  "new_body": "..."},
        {"qname": "auth.service:logout", "new_body": "..."},
        {"qname": "auth.tokens:revoke",  "new_body": "..."},
    ],
    root="/path/to/repo",
)
```

All write ops return structured `{"error": ..., "hint": ...}` dicts on failure (`not_found`, `stale_coordinates`, `syntax_error`, `write_failed`, …) so an LLM agent can treat them as recoverable and retry. Vendor scopes are read-only — write ops refuse them.

For the cases that don't fit the symbol model (imports, file lifecycle), there are dedicated ops:

```python
from snapctx.api import (
    delete_symbol,
    add_import, remove_import,
    create_file, delete_file, move_file,
)

# Drop a symbol entirely (vs edit_symbol(q, "") which corrupts spacing).
delete_symbol("auth.legacy:old_login", root="/path/to/repo")

# Add or remove an import. Idempotent: re-running with the same
# statement is a no-op. Lands at the bottom of the existing import
# block, or at the top of the file if there are no imports yet.
add_import("auth/service.py", "from .tokens import revoke", root="/path/to/repo")
remove_import("auth/service.py", "import legacy_auth", root="/path/to/repo")

# File-level lifecycle.
create_file("auth/tokens.py", "def revoke(s):\n    ...\n", root="/path/to/repo")
delete_file("auth/legacy.py", root="/path/to/repo")

# Move + identify import sites that need rewriting.
result = move_file("auth/legacy.py", "auth/legacy_v0.py", root="/path/to/repo")
for f in result["importing_files"]:
    remove_import(f, "from auth.legacy import x", root="/path/to/repo")
    add_import(f, "from auth.legacy_v0 import x", root="/path/to/repo")
```

For monorepos, the multi-root variants (`context_multi`, `search_code_multi`, `expand_multi`, `outline_multi`, `get_source_multi`) accept a list of roots and merge / route across them. The CLI uses these automatically when `discover_roots()` returns more than one root.

```python
from snapctx.api import context_multi
from snapctx.roots import discover_roots
from pathlib import Path

roots = discover_roots(".")            # walks up first; falls back to one-level walk-down
pack = context_multi("login session", roots, anchor=Path("."))
# Each seed has a "root" field tagging which sub-project it came from.
```

---

## Response shapes

### `context` (abridged)

```jsonc
{
  "query": "…",
  "mode": "hybrid",           // or "lexical" | "vector" | "exact"
  "scope": "django",          // present only for vendor-prefix queries
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
          "package": "asgiref",            // present only for cross-package edges
          "callees": [                     // depth-2 nested hop
            { "qname": "util:sanitize", "signature": "def sanitize(x)", "line": 12 }
          ]
        }
      ],
      "callers": [ /* same shape, with nested "callers" at depth 2 */ ],
      "source": "def method(self, arg: T) -> R:\n    …",
      "resolved_value": {                  // present only for constant-alias seeds
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

### `search_code`

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

### `find_literal`

```jsonc
{
  "literal": "transaction.atomic",
  "match_count": 22,
  "truncated": false,
  "matches": [
    {
      "qname": "parser.services:StrongsComparisonService.save_comparison",
      "kind": "method",
      "signature": "def save_comparison(self, …)",
      "file": "/abs/path/parser/services.py",
      "lines": "1040-1080",
      "match_line": 1056,
      "match_text": "        with transaction.atomic():",
      "source": "def save_comparison(…):\n    …",   // present with --with-bodies
      "callers": [                                   // present with --with-callers
        { "qname": "parser.views:ComparisonView.post", "line": 214 }
      ]
    }
  ],
  "hint": "22 sites found, bodies inlined. Callers attached."
}
```

---

## Security model

snapctx is a local read-mostly CLI. Worth understanding what it touches.

**What it reads:**
- Files under the discovered `.snapctx` root, and only files the walker indexed:
  - Python (`.py`, `.pyi`), TypeScript (`.ts`, `.tsx`, `.js`, `.jsx`), shell (`.sh`, `.bash`) — **not** `.env`, credentials, logs, binaries.
  - Respects `.gitignore` — anything excluded there is invisible.
  - Skips `.git`, `.venv`, `node_modules`, `__pycache__`, `.snapctx`, `dist`, `build`, `.tox` by default.
  - On-demand vendor indexing reads from `.venv/lib/python*/site-packages/<name>/` and `node_modules/<name>/` *only* when a query is prefixed with `<name>:` or `--pkg <name>` is passed. Stored separately under `.snapctx/vendor/<name>/`.

**What it writes:** Exactly one place: `<root>/.snapctx/index.db` (+ WAL/SHM sidecars). Nothing else on disk is ever touched.

**Network:** None at runtime. The ONNX embedding model is downloaded once on first `snapctx index` (into `~/.cache/huggingface/`), then every future query runs fully offline.

**Subprocess / code execution:** None. snapctx doesn't shell out, doesn't `eval`, doesn't spawn Python subprocesses. It parses with stdlib `ast` + tree-sitter, runs SQLite queries, and does ONNX inference.

**Path-traversal protection:** `outline(path=…)` only returns symbols that exist in the index. A request for `/etc/passwd` or any other off-root path returns an empty result; no filesystem read happens. `source <qname>` reads the file path stored on the matched symbol row, which the walker guarantees is under the indexed root.

**The one real caveat — same as `Read` / `Grep`:** Any secret hardcoded into a source file (API keys in module constants, credentials baked into source) becomes discoverable via the semantic search. `Read` and `Grep` already expose that; snapctx makes it *more* findable. Don't commit secrets.

**Summary:** exposure = same set of files your agent could already `Read`/`Grep`, minus all non-source content, minus anything in `.gitignore`. No network, no exec, no writes outside `.snapctx/`.

---

## What's indexed, what's not

**Indexed:**
- **Python:** functions and methods (`def`, `async def`, including nested / closure definitions); classes (with base-class list for MRO-aware `self.X` resolution); module-level constants (`UPPER_CASE = literal | identifier | collection`); class-level constants; imports.
- **TypeScript / TSX / JSX:** functions and arrow-const functions; classes (with `extends` / `implements` base chain); interfaces and type aliases; enums; React components (capitalized name + JSX in body); module-scope typed constants. JSX usage is tracked as a call edge (`<Button />` → `Button:Button`).
- **Shell (`.sh`, `.bash`):** module symbol per script (with leading-comment block as docstring); function definitions in both POSIX (`name() { … }`) and ksh (`function name { … }`) form; `source` / `.` directives as imports; intra-script function calls as call edges. External binaries (`aws`, `docker`, `git`) are intentionally skipped.
- **Module docstrings** — Python files with a top string docstring and TS/JSX files opening with a `/** … */` block. This captures the architectural "why" of a file, which often isn't repeated on any single class or function.
- **Call edges**, with optimistic `self.X` resolution through base classes, plus a post-ingest *promote* pass for forward references.

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
- Shell heredocs (`<<EOF … EOF`) aren't tracked when matching braces; a heredoc body containing an unbalanced `{` could confuse function-end detection. Rare in practice.

---

## Running the test suite

```bash
# from the repo root
uv pip install --group dev      # or: uv pip install pytest
pytest
```

205 tests pass. First run downloads the ONNX embedding model (~30 MB, cached under `~/.cache/huggingface/`).

---

## Status and roadmap

**Shipped — validated on a mixed-language monorepo (Django + Next.js, 754 files / 3,290 symbols):**
- [x] Python AST extraction — functions, methods, classes, nested scopes, module-level + class-level constants, **module docstrings**
- [x] TypeScript / TSX / JSX parser (tree-sitter) — functions, arrow consts, classes, interfaces, type aliases, enums, React components, constants, imports, JSX usage as call edges, **leading JSDoc module docs**
- [x] Shell (`.sh`, `.bash`) parser — module symbol with leading-comment docstring, POSIX + ksh function forms, `source`/`.` imports, intra-script call edges
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
- [x] **Auto-indexing on first query** — if no index is reachable, queries build one transparently before answering
- [x] **Per-repo config** — optional `snapctx.toml` at the root overrides walker defaults; no config means no behavior change
- [x] **Multi-root fan-out** — queries from a parent dir hit every indexed sub-project in parallel and tag results with their `root` label
- [x] **File watcher** (`snapctx watch`) — debounced auto re-index on save, typical run ~5 ms warm
- [x] **On-demand vendor packages with per-package isolation** — prefix a query with `<pkg>:` (or pass `--pkg <name>`) and snapctx ingests just that package into its own dedicated index. Vector neighborhoods stay focused; qnames re-root at the package. Managed via `snapctx vendor list` / `vendor forget`
- [x] **Cross-package call-graph stitching** — when a call inside one indexed package targets a name imported from another *also-indexed* package, `expand` and `context` follow the import to the sibling index and return the resolved symbol tagged with its package
- [x] **`find` — exhaustive literal-substring enumeration** over every indexed symbol body. Closes the audit-class gap vs `grep`: returns every match (not ranked, not capped), with the containing qname attached. Validated 22 / 22 vs raw `grep` on a Django audit.
- [x] **`find --with-callers`** — attaches deduped depth-1 callers to each hit so audit + impact analysis ("every X site AND who triggers them") is one call
- [x] **Audit-aware `context`** — when the query is unambiguous audit phrasing wrapping a single literal, `context` runs `find` on the literal in the same call and attaches a `find_results` block. Makes "context first" the right move even for cross-cutting audit questions
- [x] **`search --with-bodies`** for one-shot audit-class queries (inlines source with constant pre-resolution); **`search --also <term2> [...]`** to batch related terms in one call
- [x] **`outline --with-bodies`** + directory mode — exhaustive enumeration over a folder
- [x] **Smart hints** in API responses — audit-class queries get a hint nudging the agent toward `--with-bodies` or `find`

**Planned next (snapctx core):**
- [ ] **`snapctx serve` daemon** — long-running process holds the fastembed model + SQLite handle warm; CLI invocations talk to it over a Unix socket. Closes the ~400 ms cold-CLI gap so every query is 5–10 ms whether or not you have `snapctx watch` running. Lifecycle: auto-start on first query, idle-stop after N minutes, single-instance lock per repo.
- [ ] **Lazy embedder loading** — quick win that lands today's cold-CLI cost at ~50 ms for `outline`, `source`, `expand`, and any `--mode lexical` query. Doesn't help hybrid `context`, but eliminates the model load for paths that don't need it.
- [ ] **TS scope tracker** — parameter / local / import resolution so TS callee traces aren't stuck at depth 1.

**Companion projects (snaptoolkit, planned):**
- [ ] **`snapdocs`** — same idea as snapctx, applied to documentation. Index a project's docs (Markdown, MDX, RST, in-tree ADRs, even fetched third-party docs) into FTS5 + embeddings; expose a `snapdocs context "<question>"` that returns the most relevant doc passages, anchored at heading level, with the section above and below for grounding.
- [ ] **`snappatch`** — symbol-level structured editing. Where snapctx *finds* the affected symbol and its dependencies, snappatch *edits* exactly that scope and nothing else. An agent calls `snappatch edit <qname> --instruction "..."` (or feeds a unified-symbol diff); snappatch loads the symbol body + the depth-1 callers/callees that constrain the change, applies the edit, and writes back **only the affected ranges** — no whole-file rewrites.

---

## License

MIT.
