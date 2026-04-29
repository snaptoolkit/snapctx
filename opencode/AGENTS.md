# snapctx — your code-navigation and code-manipulation toolbox

This machine has `snapctx` indexes for many local repos. The `snapctx_*` tools (17 of them, exposed in this opencode session) parse code into a symbol graph, run hybrid lexical+semantic search, and provide qname-addressed write ops with syntax pre-flight. Real numbers vs `grep`/`read`/`glob`/`edit`: ~10× fewer tool calls, ~10× faster, ~10× fewer tokens for navigation; 3–4× fewer calls and 4–8× faster for refactors.

**Prefer `snapctx_*` over `grep`, `read`, `glob`, `list`, `edit`, `write` for anything code-related. The built-in tools are reserved for filename globs, whole-file binary/lockfile reads, and non-parsed text.**

## What snapctx parses

| Language | Extensions | Symbols | Calls / imports |
|---|---|---|---|
| Python | `.py`, `.pyi` | functions, methods, classes, constants, modules | yes |
| TypeScript / TSX | `.ts`, `.tsx`, `.js`, `.jsx`, `.mjs`, `.cjs` | functions, methods, classes, components, types, interfaces, constants | yes |
| Shell | `.sh`, `.bash` | functions, module-level | intra-file calls + `source`/`.` imports |
| Markdown | `.md`, `.markdown` | headings (nested as qnames) | — |
| TOML | `.toml` | top-level keys + table headers | — |
| YAML | `.yaml`, `.yml` | top-level keys | — |
| JSON | `.json` | top-level keys | — |
| Env | `.env` | `KEY=value` variables | — |

Configs, docs, and env files are first-class — they appear in `snapctx_map`, `snapctx_search`, `snapctx_outline`. Do NOT `glob`/`read` to find them.

## Mandatory first move

**In a new session, before any `glob`, `list`, `grep`, or `read`, you MUST call `snapctx_map` at least once.** It's the cheapest possible orientation — repo-wide table of contents in one call. Skipping it and globbing blind is the single biggest waste of tokens. This rule applies even when the eventual question is about non-code files (markdown, configs): `snapctx_map` shows you the repo shape so you know *which directory* matters, instead of `**/*` across the whole tree.

## qname format

Every symbol has a stable **qname**: `<module-path>:<member-path>`.

- Python: `src.snapctx.api._search:search_code` (function in `src/snapctx/api/_search.py`).
- Python method: `pkg.models:User.save`.
- TS: `src/auth/session:SessionManager.refresh`.
- Markdown: `README.md:Setup.Quickstart`.
- TOML: `pyproject.toml:project.version`.
- Module symbol (whole file): `app.urls:` — empty after the colon.

In **multi-root** sessions (opencode running at a parent of multiple indexed sub-projects, e.g. `backend/` + `frontend/`), qnames are prefixed with the root, e.g. `backend::pkg.models:User.save`. **Don't guess the prefix** — let `snapctx_search` return the canonical qname and copy it.

## Read ops — pick by question shape

| Question | Tool | Returns |
|---|---|---|
| "What's in this repo?" (orientation) | `snapctx_map` | Repo-wide table of contents grouped by directory. `depth=2` adds class methods. **Always call this first in an unfamiliar repo.** |
| "How does X work?" / "Where does Y live?" | `snapctx_context "X"` | Top-3 seed symbols with full source + callees + callers + file outlines (one shot, ~3–10 k tokens). Audit-aware: phrasings like *"every place that uses X"* trigger an exhaustive `find` in parallel. |
| "Find a symbol by name or concept (ranked)" | `snapctx_search "Y"` | Top-K ranked qnames + signatures (no bodies). `kind=function\|method\|class\|component\|interface\|type\|constant` to filter. |
| "What's in this file/dir?" | `snapctx_outline <path>` | Symbol tree (heading tree for Markdown, key list for configs, structural tree for code). |
| "Show me this exact symbol's source" | `snapctx_source <qname>` | Full body. `with_neighbors=true` adds callee signatures. |
| "Who calls X? What does X call?" | `snapctx_expand <qname> direction=both depth=2` | Call-graph neighborhood. |
| "Every place that uses literal L (inside symbols)" | `snapctx_find "L"` | Exhaustive — no top-K cap. Annotated with qname per hit. |
| "Find raw text anywhere — comments, prose, configs, env files" | `snapctx_grep "P"` | Literal or regex over **every** gitignore-respected text file. Code-file hits annotated with `qname` so you can pivot to `snapctx_source`. |

## Write ops — qname-addressed, syntax-checked, atomic per file

You don't need to read a file before editing it. Every write op:
- accepts a **qname** (or path) as the address — no line-number bookkeeping;
- runs a **syntax pre-flight** before writing (Python `ast.parse`, TS/TSX tree-sitter) and refuses edits that would leave the file unparseable;
- is **per-file atomic** — if any change in a file fails the pre-flight, none of that file's changes land (other files succeed);
- guards against **stale coordinates** — refuses if the file's SHA has drifted since the last index, telling you to re-query.

| Task | Tool | Notes |
|---|---|---|
| Replace one function / class / method body | `snapctx_edit_symbol` | `new_body` is the COMPLETE replacement (def line through last statement). |
| Insert a NEW top-level symbol next to an existing one | `snapctx_insert_symbol` | Use to add a function/class/type/component without rewriting the file. |
| Cross-symbol consistency change in one shot | `snapctx_edit_batch` | Per-file atomic. Pass an array of `{qname, new_body}`. Use for "rename a parameter everywhere", "add tracing to N functions". |
| Delete a function / class / method | `snapctx_delete_symbol` | Trims surrounding blank lines so PEP-8 / Prettier spacing stays clean. |
| Add or remove an import | `snapctx_add_import` / `snapctx_remove_import` | Idempotent. Python: docstring-aware (lands AFTER a leading module docstring). |
| Create / delete a file | `snapctx_create_file` / `snapctx_delete_file` | `create_file` runs syntax pre-flight on parser-supported languages. |
| Move / rename a file | `snapctx_move_file` | Returns `importing_files` — iterate and drive coordinated rewrites with `snapctx_add_import` / `snapctx_remove_import`. |
| Rename a symbol everywhere | `snapctx_rename_symbol` | Coordinated: def + every caller body + every import line. Refuses on collision. Filtered by the def's module suffix so unrelated namesakes are NOT touched. |

`new_body` must be the **complete** symbol source — `def`/`class`/`function` line, signature, docstring (verbatim if present), full body — exactly as it should appear in the file, with correct indentation.

## Pick the right tool — decision rules

1. **First call in a new session must be `snapctx_map`.** No exceptions.
2. Question with a concept ("how does auth work", "where is the rate limiter") → `snapctx_context`.
3. Known symbol name → `snapctx_search`, then `snapctx_source <qname>` for the body.
4. "What calls / is called by X" → `snapctx_expand`.
5. Literal you know is in code → `snapctx_find` (exhaustive, scoped to symbol bodies).
6. Literal that might live outside symbols (URLs, env names, TODOs, README prose, config keys) → `snapctx_grep`.
7. Need to **change** code → `snapctx_edit_symbol` / `insert_symbol` / `delete_symbol` / `rename_symbol` / `edit_batch`. Don't read-then-edit unless you genuinely need surrounding context.

## Recovery, not fallback

If a `snapctx_*` call returns nothing or the wrong symbol, **do NOT fall back to `read` / `glob` / `grep`**. Recover within snapctx:

- `snapctx_source` returned empty → the qname was wrong (likely a multi-root prefix issue). Run `snapctx_search "<short_name>"` to discover the canonical qname, then retry.
- `snapctx_search` returned nothing → broaden with `snapctx_context "<concept>"` (uses embeddings, tolerates paraphrase).
- `snapctx_context` returned nothing → use `snapctx_grep "<literal>"` for raw-text patterns. It walks every gitignore-respected text file and annotates code-file hits with the enclosing-symbol qname.
- `snapctx_grep` returned nothing → only THEN fall back to the built-in tools, and only after stating why snapctx couldn't help.

Reading whole files with `read` because one snapctx call missed is the failure mode this config exists to prevent.

## When to fall back to opencode's built-in tools

- **`grep`** — only for filename-pattern globs (e.g. "find every `*_test.py`"). Content search is `snapctx_grep`.
- **`read`** — only for a *whole file* end-to-end (rare). `snapctx_outline` shows structure; `snapctx_source <qname>` gives any symbol; `snapctx_grep` returns matches with N lines of context.
- **`glob`** / **`list`** — only for filename patterns. `snapctx_map` shows the directory tree.
- **`edit`** / **`write`** — only for non-code text snapctx doesn't parse (binary configs, lockfiles, generated artifacts).

## Parameter notes

- `path` / `prefix` / `in_path` are **relative to the indexed root**. Absolute paths are auto-converted by the wrapper but relative is preferred.
- `kind` filters: `function`, `method`, `class`, `component`, `interface`, `type`, `constant`, `module`.
- `snapctx_map`'s `depth` is **symbol** nesting (1 or 2), not directory depth. The full directory tree is always returned.
- `snapctx_grep`'s `regex=true` switches the pattern from literal substring to Python regex. `case_insensitive=true` works in both modes.

## Anti-patterns

- `glob("**/*.<ext>")` before `snapctx_map` — wasteful; map shows you where things live in one call, then glob the narrowed dir if you must.
- `glob("**/*<keyword>*")` to find code by concept — that's a symbol/concept query; use `snapctx_context "<keyword>"` or `snapctx_search "<keyword>"`.
- `read` on a whole file when you only want one function → use `snapctx_source <qname>`.
- `grep` for raw text anywhere → use `snapctx_grep "<pattern>"`. Same coverage with gitignore + vendor + binary filters baked in, plus qname annotation on code-file hits.
- `edit` / `write` to change a function body → use `snapctx_edit_symbol`. Syntax pre-flight catches malformed edits before they corrupt the file.
- Sequential `edit` calls on related symbols → use `snapctx_edit_batch`. Per-file atomic + one round trip.
- Renaming a symbol by hand (def + each caller + each import) → use `snapctx_rename_symbol`. One coordinated op vs the multi-step grep-edit-confirm loop.
