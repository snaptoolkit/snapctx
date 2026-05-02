# CLAUDE.md

Guide for AI agents and human contributors working on snapctx. Covers
the conventions this codebase follows so a fresh contributor can match
the existing style without re-deriving it from grep.

---

## Code exploration: dogfood snapctx

This repo IS snapctx. **Use it as your primary code-exploration tool —
prefer `snapctx context` over `Grep` / `Read` for any question about
how the codebase works.** Eating our own dog food has two purposes:

1. It's the most efficient way to navigate the codebase (10× fewer
   tokens, better recall).
2. Real-world use surfaces parser/ranker bugs we'd never find in
   tests. Past examples: `_QUERY_COMMANDS` was invisible (Python
   parser rejected tuples-of-calls) and `Button = forwardRef(...)`
   was missing (TS parser rejected call-expression RHS). Both
   surfaced and were fixed by querying snapctx-on-snapctx.

### First move for any code question

```bash
snapctx context "<your question>"
```

Examples that work well on this repo:

```bash
# Architecture question
snapctx context "how does multi-root discovery work"

# Specific function
snapctx context "discover_roots walk up to find index"

# Concept / paraphrase
snapctx context "weighted reciprocal rank fusion"

# Exact qname (fast path, ~30 ms)
snapctx context "src.snapctx.api._search:search_code"
```

Returns top symbols with full source, callees + callers (depth 2),
constant-alias resolution, and file outlines — typically enough to
answer in one call.

### Drill-down (when `context` isn't enough)

```bash
snapctx search "<query>" -k 10            # ranked symbols, no bodies
snapctx expand <qname> --direction both   # call-graph neighborhood
snapctx outline <file.py|.ts>             # file's symbol tree
snapctx source <qname> --with-neighbors   # full body + callee sigs
```

### When NOT to use snapctx

Use `Grep` for: URL strings, TODO comments, env var names, raw text
patterns, and filename globs. snapctx indexes symbols, not text.

### What just works

- **No `--root`**: walks up from CWD to find the nearest `.snapctx/index.db`.
- **Auto-refresh on every query**: SHA-keyed incremental, picks up your edits.
- **Auto-index** on first use if no index exists.
- **Stderr** for progress; **stdout** stays clean JSON for piping to `jq`.

If you ever wonder "would this be easier with snapctx?" — yes, almost
always. Try it before reaching for Grep.

---

## What snapctx is

A CLI that gives an AI agent structured context about an unfamiliar
codebase in one call. Indexer parses Python (`ast`) and TypeScript
(tree-sitter), stores symbols + calls + imports in SQLite (FTS5 +
embedding vectors), and exposes five operations: `search`, `expand`,
`outline`, `source`, `context`. The agent imports `snapctx.api` or
shells out to the `snapctx` CLI. Auto-discovery walks up to the
nearest `.snapctx/index.db`; multi-root mode fans out across a
parent-of-monorepo when no enclosing index exists.

---

## Repository layout

```
src/snapctx/
  api/             # public ops + multi-root fan-out, split by concern
  parsers/         # per-language parsers (registry-dispatched)
  config.py        # snapctx.toml loader (optional)
  walker.py        # gitignore-aware file iterator
  index.py         # SQLite schema + Repository-style accessor
  embeddings.py    # fastembed wrapper
  roots.py         # multi-root discovery
  watch.py         # debounced re-index on save
  cli.py           # argparse + dispatch table
tests/             # mirrors src layout; tmp_path fixtures
snapctx.toml       # optional, per-repo config (none in this repo)
```

`api/` is a package, not a single file: each operation lives in its
own submodule (`_search.py`, `_graph.py`, `_context.py`, etc.) and
`api/__init__.py` re-exports the public surface. **Don't grow a
single module past ~400 lines** — split when one file is doing more
than one job.

---

## Coding conventions

### Comments

Default to **no comments**. Add one only when the *why* is non-obvious:
a hidden constraint, a subtle invariant, a workaround for a specific
bug, behavior that would surprise a reader.

Don't:
```python
# Increment counter
counter += 1
```

Do (when the why matters):
```python
# Demote runs BEFORE promote so bogus MRO guesses (e.g. self.x
# guessed against an imported base) are nulled first.
demoted = idx.demote_unresolved_calls()
idx.promote_self_calls()
```

Don't reference the current task or PR in comments (that belongs in
the commit message). Comments rot; commit messages are immutable.

### Docstrings

Module docstrings explain why the module exists and what it owns —
not just what it contains. Cite empirical evidence when explaining
tunables ("vec_weight=1.5 — embeddings beat BM25 on identifier-heavy
queries because BM25's camel/snake token splits mix real matches with
noise"). Use reST-style code blocks (```` ``foo`` ````) for inline
identifiers.

Function docstrings cover edge cases and non-obvious behavior, not
the signature: types and parameter names speak for themselves.

### Naming

- Public functions in `api/` are unprefixed (`search_code`, `expand`).
- Helpers used by one or two siblings get a leading underscore
  (`_fan_out`, `_route_qname`).
- Constants in `SCREAMING_SNAKE_CASE`.
- Frozen dataclasses for config-shaped values (`WalkerConfig`,
  `QueryCommand`); regular dataclasses for transient state.

### Module size and Single Responsibility

A file > ~400 lines is a smell. The split happens when one module is
doing more than one thing. Past examples:

- `api.py` (1283 lines) was search + ranking + graph walk + indexing
  + multi-root fan-out → split into 9 focused submodules.
- Parsers (Python 623 + TS 779) are NOT split because each does one
  thing (parse one language) — size alone isn't the trigger.

### Error handling

- Validate at boundaries: config files, user input, file paths.
- Trust internal calls — don't add `try/except` for things that
  can't happen (a function returning `dict` returning `None` instead).
- Stderr for progress and errors; stdout stays clean for JSON. CLI
  callers pipe stdout to `jq`; warnings on stdout would break that.
- One bad sub-project shouldn't poison a multi-root response — surface
  it in `root_errors` and keep going.

### Imports

- Top-of-file for normal deps.
- Lazy (function-local) for: heavy modules (`fastembed`, `re` in
  hot paths), circular-import avoidance (`from snapctx.roots import
  ...` inside `_multi.py`), or feature-gated paths.

---

## Testing

### Naming

Test names describe the scenario, not the SUT. `test_walk_up_takes_
precedence_over_walk_down` beats `test_discovery_2`. The name should
make the assertion's *why* obvious without reading the body.

### Fixtures

Use `tmp_path` for filesystem tests. Build minimal fixtures inline —
a 3-line `.py` file is more readable than a fixture directory. Use
the shared `indexed_root` fixture in `conftest.py` for tests that
need a pre-built index.

### When to add a regression test

Always, when fixing a bug. The test should fail on the pre-fix code
and pass on the post-fix code. Without this, the bug returns 6 months
later.

When adding a feature, write tests that capture the contract — the
behavior callers depend on. Resist tests that lock in implementation
details (which fields a private function returns), since those churn
on every refactor.

### Perf tests

Opt-in via `RUN_PERF=1` env var. Default-skipped because they generate
synthetic fixtures and exercise the embedder. Thresholds are generous
(3× observed) so they catch regressions, not flake on a slow machine.

### Test count is currency

We have 123 tests across ~20 test files. Adding a meaningful test is
cheap; deleting an unmeaningful one is fine too. Don't write tests
just to bump the count.

---

## Architecture preferences

### Adding a new parser

1. Implement `parsers/base.Parser` in `parsers/<lang>.py`.
2. Add to `_PARSERS` in `parsers/registry.py`.
3. Tests in `tests/test_<lang>_parser.py`.

The protocol is intentionally minimal: emit `Symbol`, `Call`, `Import`
records. Indexing, search, ranking, the call-graph passes, and
`context()` are all language-agnostic — the parser is the only place
language knowledge lives.

**Don't** prematurely extract a base class for shared parser logic.
The Python parser uses stdlib `ast`; TS uses tree-sitter; the shared
"shape" is a coincidence. Wait until language #3 to identify what's
genuinely common.

### Adding a CLI query command

Add an entry to `_QUERY_COMMANDS` in `cli.py`:

```python
QueryCommand(
    "newcmd", new_op, new_op_multi,
    arg_names=("query", "k"),
),
```

If the new command also needs a multi-root variant, follow the
patterns in `api/_multi.py` (merge for fan-out + score, route for
qname/path-based ops).

### Adding a config knob

The bar is "I have a real repo where the default fails me", not
"this might be useful someday." If you can't name two real projects
that need the knob, don't add it.

When you do: every key is optional, defaults match prior behavior
(no breaking change for users without a config file), unknown keys
are tolerated for forward compatibility.

### Multi-root operations

Every public op has a `*_multi` variant in `api/_multi.py`. The CLI
picks single vs multi based on `len(roots)`. Two patterns:

- **Merge** (`search`, `context`): fan out → tag with root → sort by
  score → top-K. Use `_fan_out` for the parallel + error-capture
  scaffolding.
- **Route** (`expand`, `source`): pick the owning root → delegate →
  tag. Use `_route_qname` for symbol-routing.

### Auto-discovery rules

Walk up first, walk down one level on miss. Stop at the first
`.snapctx/index.db`. Multi-root only fires when no enclosing index
exists *and* multiple children are indexed. Don't deepen walk-down
past one level — `node_modules` will eat you.

### Auto-indexing

Query commands auto-index when no index is reachable AND the
directory contains parseable source files. Pre-flight is the cheap
"any source files?" check via the walker — don't create stub
`.snapctx/` directories in unrelated paths.

**Monorepo parent detection.** When the anchor has no project marker
of its own but ≥2 immediate-child dirs do (`pyproject.toml`,
`package.json`, `Cargo.toml`, etc. — see `roots.PROJECT_MARKERS`),
auto-index *each child* as its own root rather than indexing the
anchor as one big index. Same logic also extends walk-down: if
discovery found one indexed sub-project but other marker'd siblings
aren't yet indexed, auto-index those too so the multi-root response
covers everything. Marker presence is the signal that lets us
distinguish a real monorepo parent from a regular repo where
`src/` and `tests/` happen to both have source — neither has a
project marker, so anchor-bootstrap wins as before.

---

## Workflow

### Build → measure → refactor

Ship the feature. Measure (real repo, real query). Then refactor.
Don't pre-emptively abstract for hypothetical future requirements.
Past wins followed this order:

- Vendor-bundle filter: shipped → measured 85s → fixed walker → 10s.
- Embedding batch size: shipped at default 256 → measured padding
  cost → batch=4 → 3× faster.
- Auto-index: shipped explicit → users hit the gap → added.

### Refactor signals

Refactor when one of these is true:

- A module is > 400 lines and serves more than one concern.
- Adding a small feature requires touching 3+ unrelated files.
- A test name describes pure orchestration ("test that flag X is
  passed through layer A, B, C").
- A bug fix has to be made twice in two near-identical places.

Don't refactor for tidiness alone. Code that works and reads cleanly
is fine, even if it could be slightly more elegant.

### Commit before refactoring

Always commit working code before starting a refactor. The diff
should be reviewable as "no behavior change" — and you can only
prove that against a clean baseline.

### Commit messages

Multi-paragraph, why-focused. First line ≤ 72 chars summarizing the
intent. Body explains motivation, not the diff (which `git diff`
already shows). Reference test counts (`123 passed`) when meaningful.
Use the HEREDOC pattern to avoid quoting issues.

---

## Anti-patterns

Things that look helpful but aren't:

- **Forward-compatibility shims** — until someone needs them. Renaming
  a thing? Just rename it. Don't leave the old name as an alias for
  six months "just in case."
- **Validation of internal trust boundaries** — if you know the type
  is `Path` because you constructed it, don't `isinstance`-check it.
- **Wrapping every external call in try/except** — let the exception
  propagate unless you know what to do with it.
- **Comments that paraphrase the code** — they rot, they lie, they
  add noise.
- **Premature base classes / interfaces** — abstraction is a tax;
  pay it when there are 3+ implementations, not 2.
- **Ranker-weight knobs in config** — those are tuning decisions.
  They live in code (with empirical justification in a docstring),
  not user-facing config.
- **Eager imports of heavy modules** — `fastembed` takes 250 ms to
  import. Lazy-import it inside the function that needs it.

---

## When the agent (Claude) is doing the work

A few things specific to AI-driven development on this codebase:

- **Read before writing**. Especially in `api/_*` — the modules are
  small and tight; introducing a duplicate helper because you didn't
  see the existing one is the most common error.
- **Run the test suite before committing**. `.venv/bin/python -m pytest
  tests/` should print "123 passed" (or whatever the current count is).
  No PR ships with regressions.
- **One concern per commit**. If you're refactoring AND fixing a bug
  AND adding a feature, that's three commits.
- **Stash architectural opinions for the user**. If a request asks
  for `X` and you think `Y` is better, ship `X` first, then briefly
  flag `Y` afterward. Don't rebel mid-task.
- **Ask before destructive operations**: `rm -rf`, `git reset --hard`,
  `git push --force`, anything that drops data. Building tests, files,
  packages — proceed.
