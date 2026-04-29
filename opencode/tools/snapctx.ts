import { tool } from "@opencode-ai/plugin"
import { spawn } from "node:child_process"
import { homedir } from "node:os"
import { relative, isAbsolute, join, dirname } from "node:path"
import { fileURLToPath } from "node:url"

function relPath(p: string, cwd: string): string {
  if (!isAbsolute(p)) return p
  const r = relative(cwd, p)
  return r === "" ? "." : r
}

// Resolve the effective working directory for a tool call. By default
// every snapctx_* tool acts on the opencode session's cwd
// (``ctx.directory``). Passing ``root`` redirects the call to a
// different checkout — typically a git worktree under ``/tmp/<name>``
// — so refactors can be staged in isolation without touching the main
// workspace. Issue #18.
function resolveCwd(args: { root?: string }, ctx: { directory: string }): string {
  if (!args.root) return ctx.directory
  return isAbsolute(args.root) ? args.root : join(ctx.directory, args.root)
}

const ROOT_ARG_DESC =
  "Override the indexed root for this call (absolute, or relative to the session cwd). Use to target a git worktree outside the main workspace. Defaults to the session cwd."

// Override any of these with environment variables:
//   SNAPCTX_BIN     — path to the ``snapctx`` CLI
//   SNAPCTX_PYTHON  — interpreter that has ``snapctx`` installed
//   SNAPCTX_BRIDGE  — path to ``_snapctx_writer.py``
// Defaults are set to the local uv tool install so write ops work even when
// plain `python3` does not have `snapctx` importable.
const SNAPCTX = process.env.SNAPCTX_BIN || join(homedir(), ".local", "bin", "snapctx")
const PYTHON =
  process.env.SNAPCTX_PYTHON ||
  join(
    homedir(),
    ".local",
    "share",
    "uv",
    "tools",
    "snapctx",
    "bin",
    "python",
  )
const BRIDGE =
  process.env.SNAPCTX_BRIDGE ||
  join(
    (() => {
      try {
        return dirname(fileURLToPath(import.meta.url))
      } catch {
        return join(homedir(), ".config", "opencode", "tools")
      }
    })(),
    "_snapctx_writer.py",
  )

function callApi(op: string, args: Record<string, unknown>, cwd: string): Promise<string> {
  // Bridge sets ``root=cwd`` on the Python call; if a wrapper's
  // ``args`` carries its own ``root`` (the per-call worktree
  // override) we strip it here so the kwarg doesn't collide.
  const { root: _root, ...payload } = args
  return new Promise((resolve, reject) => {
    const p = spawn(PYTHON, [BRIDGE], { cwd })
    let out = ""
    let err = ""
    p.stdout.on("data", (d) => (out += d.toString()))
    p.stderr.on("data", (d) => (err += d.toString()))
    p.on("close", (code) => {
      if (code === 0) resolve(out.trim())
      else reject(new Error(`snapctx ${op} failed (${code}): ${out.trim() || err.trim()}`))
    })
    p.on("error", reject)
    p.stdin.write(JSON.stringify({ op, args: payload, root: cwd }))
    p.stdin.end()
  })
}

function run(args: string[], cwd: string): Promise<string> {
  return new Promise((resolve, reject) => {
    const p = spawn(SNAPCTX, args, { cwd })
    let out = ""
    let err = ""
    p.stdout.on("data", (d) => (out += d.toString()))
    p.stderr.on("data", (d) => (err += d.toString()))
    p.on("close", (code) => {
      if (code === 0) resolve(out)
      else reject(new Error(`snapctx ${args[0]} exited ${code}: ${err.trim()}`))
    })
    p.on("error", reject)
  })
}

export const context = tool({
  description:
    "PREFERRED FIRST MOVE for any code question. One-shot symbol-level retrieval: ranked seeds + their source + callees + callers + file outlines. Use this INSTEAD of grep/read/glob when the question is about how code works, where a concept lives, or what calls what. Auto-indexes on first use; auto-refreshes on every call.",
  args: {
    query: tool.schema
      .string()
      .describe("Natural-language question or qname. Examples: 'how does auth middleware work', 'src.app.auth:verify_token'."),
    k_seeds: tool.schema
      .number()
      .optional()
      .describe("Number of seed symbols to expand (default 3)."),
    root: tool.schema.string().optional().describe(ROOT_ARG_DESC),
  },
  async execute(args, ctx) {
    const cwd = resolveCwd(args, ctx)
    const cli = ["context", args.query]
    if (args.k_seeds) cli.push("--k-seeds", String(args.k_seeds))
    return await run(cli, cwd)
  },
})

export const search = tool({
  description:
    "Ranked symbol search (BM25+vector hybrid). Returns qnames + signatures, no bodies. Prefer over grep when looking for a symbol by concept or partial name.",
  args: {
    query: tool.schema.string().describe("Query string."),
    k: tool.schema.number().optional().describe("Top-K results (default 10)."),
    kind: tool.schema
      .string()
      .optional()
      .describe("Filter by kind: function, class, method, module."),
    root: tool.schema.string().optional().describe(ROOT_ARG_DESC),
  },
  async execute(args, ctx) {
    const cwd = resolveCwd(args, ctx)
    const cli = ["search", args.query]
    if (args.k) cli.push("-k", String(args.k))
    if (args.kind) cli.push("--kind", args.kind)
    return await run(cli, cwd)
  },
})

export const outline = tool({
  description:
    "Symbol tree of a single file or directory. Prefer over `read` when you only need to see what's in a file, not the bodies.",
  args: {
    path: tool.schema.string().describe("File or directory path."),
    root: tool.schema.string().optional().describe(ROOT_ARG_DESC),
  },
  async execute(args, ctx) {
    const cwd = resolveCwd(args, ctx)
    return await run(["outline", relPath(args.path, cwd)], cwd)
  },
})

export const source = tool({
  description:
    "Full source of a single symbol by qname. Prefer over `read` of a whole file when you only need one function/class.",
  args: {
    qname: tool.schema.string().describe("Qualified name, e.g. 'src.app.auth:verify_token'."),
    with_neighbors: tool.schema
      .boolean()
      .optional()
      .describe("Include callee signatures."),
    root: tool.schema.string().optional().describe(ROOT_ARG_DESC),
  },
  async execute(args, ctx) {
    const cwd = resolveCwd(args, ctx)
    const cli = ["source", args.qname]
    if (args.with_neighbors) cli.push("--with-neighbors")
    return await run(cli, cwd)
  },
})

export const expand = tool({
  description:
    "Walk the call graph around a qname (callees / callers / both). Prefer over grepping for call sites.",
  args: {
    qname: tool.schema.string().describe("Qualified symbol name."),
    direction: tool.schema
      .string()
      .optional()
      .describe("'callees', 'callers', or 'both' (default 'both')."),
    depth: tool.schema.number().optional().describe("Walk depth (default 2)."),
    root: tool.schema.string().optional().describe(ROOT_ARG_DESC),
  },
  async execute(args, ctx) {
    const cwd = resolveCwd(args, ctx)
    const cli = ["expand", args.qname]
    if (args.direction) cli.push("--direction", args.direction)
    if (args.depth) cli.push("--depth", String(args.depth))
    return await run(cli, cwd)
  },
})

export const find = tool({
  description:
    "Exhaustive literal-substring search across indexed symbol bodies. Use this for raw text patterns (URLs, env var names, TODO markers) where symbol search wouldn't help. Still prefer over `grep` since results are scoped to parsed symbols, not generated/vendored noise.",
  args: {
    literal: tool.schema.string().describe("Exact substring to find."),
    in_path: tool.schema.string().optional().describe("Restrict to a path."),
    kind: tool.schema.string().optional().describe("Filter by symbol kind."),
    root: tool.schema.string().optional().describe(ROOT_ARG_DESC),
  },
  async execute(args, ctx) {
    const cwd = resolveCwd(args, ctx)
    const cli = ["find", args.literal]
    if (args.in_path) cli.push("--in", relPath(args.in_path, cwd))
    if (args.kind) cli.push("--kind", args.kind)
    return await run(cli, cwd)
  },
})

export const grep = tool({
  description:
    "Literal or regex search over EVERY text file under the root — markdown, configs (TOML/YAML/JSON/.env), code, plain text. Hits inside parsed code files are annotated with the enclosing-symbol qname so you can pivot to snapctx_source. PREFER this over the generic grep/read tool: same coverage, gitignore + vendor + binary filters built in, plus qname annotation.",
  args: {
    pattern: tool.schema
      .string()
      .describe("Literal substring (default) or regex (set regex=true)."),
    regex: tool.schema
      .boolean()
      .optional()
      .describe("Treat pattern as a Python regex. Default false (literal)."),
    in_path: tool.schema
      .string()
      .optional()
      .describe("Restrict the walk to files under this path (relative or absolute)."),
    case_insensitive: tool.schema
      .boolean()
      .optional()
      .describe("Case-insensitive match."),
    context_lines: tool.schema
      .number()
      .optional()
      .describe("Lines of context before/after each hit (default 1, 0 to disable)."),
    max_results: tool.schema
      .number()
      .optional()
      .describe("Cap on total hits (default 200)."),
    root: tool.schema.string().optional().describe(ROOT_ARG_DESC),
  },
  async execute(args, ctx) {
    const cwd = resolveCwd(args, ctx)
    const cli = ["grep", args.pattern]
    if (args.regex) cli.push("--regex")
    if (args.case_insensitive) cli.push("-i")
    if (args.in_path) cli.push("--in", relPath(args.in_path, cwd))
    if (args.context_lines !== undefined) cli.push("-C", String(args.context_lines))
    if (args.max_results) cli.push("--max-results", String(args.max_results))
    return await run(cli, cwd)
  },
})

export const map = tool({
  description:
    "Repo-wide table of contents — every indexed file grouped by directory with top-level symbols. Use as ORIENTATION when first dropping into an unfamiliar repo, before any grep/glob/ls.",
  args: {
    prefix: tool.schema.string().optional().describe("Restrict to a path prefix."),
    depth: tool.schema
      .number()
      .optional()
      .describe(
        "Symbol nesting depth (1 or 2). 1 = top-level symbols only (default). 2 = also include class methods / nested functions. Does NOT control directory depth — the full directory tree is always returned.",
      ),
    root: tool.schema.string().optional().describe(ROOT_ARG_DESC),
  },
  async execute(args, ctx) {
    const cwd = resolveCwd(args, ctx)
    const cli = ["map"]
    if (args.prefix) cli.push("--prefix", relPath(args.prefix, cwd))
    if (args.depth) {
      const d = Math.min(2, Math.max(1, args.depth))
      cli.push("--depth", String(d))
    }
    return await run(cli, cwd)
  },
})
export const edit_symbol = tool({
  description:
    "Replace a symbol's body by qname. Per-file atomic, runs syntax pre-flight before writing. Prefer over opencode's `write`/`edit` when changing one function/method/class — you don't need to read the file first.",
  args: {
    qname: tool.schema.string().describe("Qualified symbol name to replace."),
    new_body: tool.schema
      .string()
      .describe(
        "COMPLETE replacement body, including the `def`/`class` line, signature, docstring (if any), and full implementation. Indented as it should appear in the file.",
      ),
    root: tool.schema.string().optional().describe(ROOT_ARG_DESC),
  },
  async execute(args, ctx) {
    return await callApi("edit_symbol", args, resolveCwd(args, ctx))
  },
})

export const insert_symbol = tool({
  description:
    "Insert a NEW top-level symbol adjacent to an anchor symbol (before/after). Use to add a new function/class without rewriting the file. Syntax pre-flight applies. The anchor's qname locates the file — no `file` argument needed.",
  args: {
    anchor_qname: tool.schema
      .string()
      .describe("Qname of the existing symbol to insert near. Locates the file too."),
    position: tool.schema
      .string()
      .describe("'before' or 'after' the anchor."),
    new_text: tool.schema
      .string()
      .describe("Complete new symbol source, including `def`/`class` line."),
    root: tool.schema.string().optional().describe(ROOT_ARG_DESC),
  },
  async execute(args, ctx) {
    return await callApi("insert_symbol", args, resolveCwd(args, ctx))
  },
})

export const edit_batch = tool({
  description:
    "Apply MANY symbol edits in ONE call. Per-file atomic: if any edit in a file fails syntax pre-flight, NO edits to that file land (other files succeed). Use for cross-symbol consistency changes (rename a parameter everywhere, add tracing to several functions).",
  args: {
    edits: tool.schema
      .array(
        tool.schema.object({
          qname: tool.schema.string(),
          new_body: tool.schema.string(),
        }),
      )
      .describe("List of {qname, new_body} edits."),
    root: tool.schema.string().optional().describe(ROOT_ARG_DESC),
  },
  async execute(args, ctx) {
    return await callApi("edit_symbol_batch", { edits: args.edits }, resolveCwd(args, ctx))
  },
})

export const delete_symbol = tool({
  description:
    "Delete a symbol by qname. Trims surrounding blank lines to preserve PEP-8 spacing. Refuses if the file would no longer parse.",
  args: {
    qname: tool.schema.string().describe("Qualified symbol name to delete."),
    root: tool.schema.string().optional().describe(ROOT_ARG_DESC),
  },
  async execute(args, ctx) {
    return await callApi("delete_symbol", args, resolveCwd(args, ctx))
  },
})

export const add_import = tool({
  description:
    "Add an import line to a file. Idempotent (no-op if already present). Docstring-aware: places the import after a leading module docstring, not above it.",
  args: {
    file: tool.schema.string().describe("File path (relative to root)."),
    statement: tool.schema
      .string()
      .describe(
        "The full import line, e.g. 'from typing import Any' or 'import json'.",
      ),
    root: tool.schema.string().optional().describe(ROOT_ARG_DESC),
  },
  async execute(args, ctx) {
    return await callApi("add_import", args, resolveCwd(args, ctx))
  },
})

export const remove_import = tool({
  description:
    "Remove an import line from a file. Idempotent (no-op if not present).",
  args: {
    file: tool.schema.string().describe("File path (relative to root)."),
    statement: tool.schema.string().describe("The exact import line to remove."),
    root: tool.schema.string().optional().describe(ROOT_ARG_DESC),
  },
  async execute(args, ctx) {
    return await callApi("remove_import", args, resolveCwd(args, ctx))
  },
})

export const create_file = tool({
  description:
    "Create a new file with content. Refuses if file exists or content has a syntax error. Re-indexes after.",
  args: {
    path: tool.schema.string().describe("New file path (relative to root)."),
    content: tool.schema.string().describe("File contents."),
    root: tool.schema.string().optional().describe(ROOT_ARG_DESC),
  },
  async execute(args, ctx) {
    return await callApi("create_file", args, resolveCwd(args, ctx))
  },
})

export const delete_file = tool({
  description:
    "Delete a file. Refuses if outside root. Re-indexes after.",
  args: {
    path: tool.schema.string().describe("File path to delete (relative to root)."),
    root: tool.schema.string().optional().describe(ROOT_ARG_DESC),
  },
  async execute(args, ctx) {
    return await callApi("delete_file", args, resolveCwd(args, ctx))
  },
})

export const move_file = tool({
  description:
    "Move/rename a file. Returns `importing_files` so you can drive coordinated import-path rewrites in callers (use add_import / remove_import or edit_symbol).",
  args: {
    src: tool.schema.string().describe("Current path (relative to root)."),
    dst: tool.schema.string().describe("New path (relative to root)."),
    root: tool.schema.string().optional().describe(ROOT_ARG_DESC),
  },
  async execute(args, ctx) {
    return await callApi("move_file", args, resolveCwd(args, ctx))
  },
})

export const rename_symbol = tool({
  description:
    "Coordinated rename: change a symbol's def + every caller body + every import line in one op. Word-boundary substitution; refuses on collision (target qname already exists). Filters imports by the def's module suffix to avoid renaming unrelated namesakes in other modules.",
  args: {
    old_qname: tool.schema.string().describe("Current qname of the symbol."),
    new_name: tool.schema
      .string()
      .describe(
        "New SHORT name only (e.g. 'compute_total'), not a full qname. Module path is preserved.",
      ),
    root: tool.schema.string().optional().describe(ROOT_ARG_DESC),
  },
  async execute(args, ctx) {
    return await callApi("rename_symbol", args, resolveCwd(args, ctx))
  },
})
