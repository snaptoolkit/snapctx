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
| HTML / templates | `.html`, `.htm`, `.j2`, `.jinja`, `.jinja2`, `.liquid`, `.njk`, `.twig`, `.hbs`, `.handlebars`, `.mustache` | `<title>` + `<h1>`..`<h6>`; module docstring = stripped prose so prompts/instructions are searchable via embeddings | — |
| Plain text | `.txt` | one module symbol; docstring = leading prose | — |
| TOML | `.toml` | top-level keys + table headers | — |
| YAML | `.yaml`, `.yml` | top-level keys | — |
| JSON | `.json` | top-level keys | — |
| Env | `.env` | `KEY=value` variables | — |

Configs, docs, and env files are first-class — they appear in `snapctx_map`, `snapctx_search`, `snapctx_outline`. Do NOT `glob`/`read` to find them.

## First move: pick the right tool, don't always start with map

Match the *shape of the question* to the right tool. Most queries skip orientation entirely:

| Question shape | First call | Why |
|---|---|---|
| **Known symbol name** (a function, class, type, or component name you already have) | `snapctx_search "<name>" -k 5` with the right `kind` (see [Kind filter](#kind-filter-cheat-sheet)) | The fastest path when you have the name. If `kind` was wrong, snapctx retries without it automatically and tells you the actual kind. |
| **Literal / config key / env var / token / route fragment** (any exact string you'd `grep` for) | `snapctx_grep "<literal>" --in-path <subtree>` | Path-scoped grep is dramatically faster and cleaner than broad search. **Always pass `in_path`** when you have a directory hint. |
| **Workflow / pipeline question with likely docs** (a feature that probably has a README or design doc) | `snapctx_source "<doc-path>:<heading>"` if you can guess the doc, otherwise `snapctx_context "<query>"` | Docs prose is indexed. Going straight to the doc heading + one implementation symbol is faster than crawling the whole feature. |
| **Open-ended feature / concept** (you don't have a name; you don't know the area) | `snapctx_context "<query>"` | Returns top seeds with full source + callees + callers + file outlines in one shot. Best when the source area is broad or unknown. Self-trims on overflow (sets `trimmed: "soft"` or `"hard"` and emits a scope-down hint) — when you see that, follow the hint with `snapctx_grep --in-path` or `snapctx_search`. |
| **Repo with framework build artifacts** (any `.next/`, `dist/`, `.svelte-kit/`, etc. in tree) | path-scoped `snapctx_grep` first, NOT broad context | snapctx skips standard build dirs by default, but path-scoping is still the safest move when the codebase has lots of generated noise. |
| **Genuinely don't know the repo's shape** | `snapctx_map` | Repo-wide table of contents in one call. Lean by default; pass `mode=full` for signatures. |

After the first call, pivot:
- Got a qname or path back? → `snapctx_source <qname>` for the body, or `snapctx_outline <file>` for the tree.
- Got a function and want to know what it calls / is called by? → `snapctx_expand <qname> direction=both depth=2`.
- Need to find every place a literal is used? → `snapctx_find "<literal>"` (exhaustive, scoped to symbol bodies).

**`snapctx_map` is no longer the universal first move.** It's the right call when you genuinely don't know the repo shape, or when you've just been dropped into an unfamiliar monorepo. For focused questions, the table above is faster.

## Kind filter cheat sheet

`snapctx_search` accepts `kind=<value>` to narrow results. If the kind is wrong, snapctx detects it two ways:

- **No results** → it retries without the filter and tells you the actual kinds it found.
- **Drift** (results came back but none have your exact name) → it surfaces a `kind_suggestion` field naming the canonical symbol with that exact name and its actual kind.

Either way you don't need to re-query blind. Common confusions:

| Language | Top-level def → kind | Class member → kind | Type / interface → kind | Component → kind |
|---|---|---|---|---|
| Python | `function` | `method` | (no separate kind) | — |
| TypeScript / TSX | `function` | `method` | `type` for `type X = …`, `interface` for `interface X` | `component` for React (`function`/`class` exporting JSX) |
| JavaScript / JSX | `function` | `method` | — | `component` |

Other kinds: `class`, `constant` (SCREAMING_SNAKE), `module` (whole-file).

## Don't delegate to subagents

**Do NOT spawn subagents (`task`, `explore`, or any agent-delegation tool) for code exploration.** Handle the whole question in this thread with `snapctx_*` calls. A subagent starts cold, can't see what you've already learned, and tends to fall back to `grep`/`read`/`glob` — which is exactly the loop this config exists to prevent. The whole point of `snapctx_map` + `snapctx_context` is that one direct call already returns more useful structure than a fresh subagent would gather in ten. If a question feels big enough to delegate, it's big enough to deserve `snapctx_context "<query>"` — try that first.

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
| "What's in this repo?" (orientation, when you genuinely don't know the shape) | `snapctx_map` | Repo-wide table of contents grouped by directory. Lean by default (qname + kind + 1-line docstring + decorators); pass `mode=full` to also get signatures and line ranges, or call `snapctx_outline <file>` on a specific file when you need that detail. `depth=2` adds class methods. Skip this and go straight to `snapctx_context` / `snapctx_search` / `snapctx_grep` whenever the [decision rule](#first-move-pick-the-right-tool-dont-always-start-with-map) gives you a more focused first call. |
| "How does X work?" / "Where does Y live?" | `snapctx_context "X"` | Top-3 seed symbols with full source + callees + callers + file outlines (one shot, ~3–10 k tokens). Audit-aware: phrasings like *"every place that uses X"* trigger an exhaustive `find` in parallel. |
| "Find a symbol by name or concept (ranked)" | `snapctx_search "Y"` | Top-K ranked qnames + signatures (no bodies). `kind=function\|method\|class\|component\|interface\|type\|constant` to filter. |
| "What's in this file/dir?" | `snapctx_outline <path>` | Symbol tree (heading tree for Markdown, key list for configs, structural tree for code). |
| "Show me this exact symbol's source" | `snapctx_source <qname>` | Full body. `with_neighbors=true` adds callee signatures. |
| "Who calls X? What does X call?" | `snapctx_expand <qname> direction=both depth=2` | Call-graph neighborhood. |
| "Every place that uses literal L (inside symbols)" | `snapctx_find "L"` | Exhaustive — no top-K cap. Annotated with qname per hit. |
| "Find raw text anywhere — comments, prose, configs, env files" | `snapctx_grep "P" --in-path <dir>` | Literal or regex over every gitignore-respected text file. Code-file hits annotated with `qname`. **Always pass `in_path`** if you have a directory hint — 10× faster on monorepos and dramatically cleaner results. By default ranks **definition lines first** (where `P` is `def`/`class`/`function`/`const`/etc. introduced) then usage lines, so "where is X defined" surfaces immediately. Each match carries a `definition: bool` flag. Pass `--no-definitions-first` for natural file-order audits. |

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

## Path-scoping with `in_path`

`snapctx_grep` and `snapctx_find` both accept an `in_path` parameter that scopes the scan to a subtree. **Use it whenever you have a directory hint** — on a multi-subproject monorepo, the difference between unscoped and scoped is often 10× speed plus dramatically cleaner results (no false hits in unrelated subprojects/migrations/fixtures):

```
snapctx_grep "<TOKEN>" --in-path <subdir>      # scoped — fast, focused
snapctx_grep "<TOKEN>"                          # unscoped — slower, noisier
```

How to pick the path: use `snapctx_map` once to learn the top-level layout (or recall it from the previous call), then scope to whichever subtree the question lives in — the directory containing the relevant subproject, the feature area, the framework's config dir, etc. Even a one-level scope (`--in-path src` vs nothing) noticeably improves precision on large repos.

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
- `snapctx_map`'s `mode` defaults to `lean` (no per-symbol signatures or line ranges) so the orientation payload stays small. Set `mode=full` only when you actually need signatures from map; otherwise `snapctx_outline <file>` is the right next call.
- `snapctx_grep`'s `regex=true` switches the pattern from literal substring to Python regex. `case_insensitive=true` works in both modes.

## Anti-patterns

- `glob("**/*.<ext>")` to discover where files live — wasteful; `snapctx_map` shows it in one call (or `snapctx_outline <dir>` for a single subtree).
- `glob("**/*<keyword>*")` to find code by concept — that's a symbol/concept query; use `snapctx_context "<keyword>"` or `snapctx_search "<keyword>"`.
- `snapctx_grep "<token>"` without `--in-path` when you have a directory hint — wastes time scanning unrelated subtrees and pollutes results. Always scope when you can.
- `snapctx_search "<name>"` with no `kind` when you know the kind — pass `kind=method` (Python class member), `kind=component` (React), etc. Snapctx auto-retries on empty result, but the right `kind` is always cheaper.
- `read` on a whole file when you only want one function → use `snapctx_source <qname>`.
- `grep` for raw text anywhere → use `snapctx_grep "<pattern>"`. Same coverage with gitignore + vendor + binary filters baked in, plus qname annotation on code-file hits.
- `edit` / `write` to change a function body → use `snapctx_edit_symbol`. Syntax pre-flight catches malformed edits before they corrupt the file.
- Sequential `edit` calls on related symbols → use `snapctx_edit_batch`. Per-file atomic + one round trip.
- Renaming a symbol by hand (def + each caller + each import) → use `snapctx_rename_symbol`. One coordinated op vs the multi-step grep-edit-confirm loop.
- `task` / `explore` subagents for code questions → handle inline with `snapctx_context`. Subagents lose your accumulated context and revert to `grep`/`read` habits in their fresh thread.
