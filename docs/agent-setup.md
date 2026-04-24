# Agent setup guide

How to wire `neargrep` into an AI coding agent so the agent actually reaches for it first.

Two pieces:

1. **Register the MCP server** with the client (Claude Code / Cursor / Cline / custom).
2. **Tell the agent when to use it** via `CLAUDE.md` / `AGENTS.md` / system prompt — otherwise the agent will default to `Grep` + `Read` out of habit.

---

## 1. Register the MCP server

### Claude Code

Create or edit `.mcp.json` at your repo root:

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

- The command assumes `neargrep-mcp` is on your `PATH` (it is after `uv tool install --editable .`).
- `--root .` resolves to the project root — the client's working directory when it spawns the server.
- On next launch, `/mcp` in Claude Code lists the server and its five tools.

If you prefer an absolute path (e.g. running from multiple shells):

```json
{
  "mcpServers": {
    "neargrep": {
      "command": "/Users/you/.local/bin/neargrep-mcp",
      "args": ["--root", "/absolute/path/to/your/repo"]
    }
  }
}
```

### Cursor / Cline / other MCP clients

Same shape in each client's MCP config panel. The server name (`neargrep`) determines the tool-call prefix — Claude Code exposes them as `mcp__neargrep__context`, `mcp__neargrep__search`, etc.

### Custom Agent SDK

Python (Anthropic Agent SDK, LangGraph, etc.) can consume via the MCP client libraries, or — for less overhead — import `neargrep.api` directly as a Python library. See the README "Full API" section.

Google ADK: either use `MCPToolset` pointing at `neargrep-mcp` (subprocess), or wrap `neargrep.api` functions in `FunctionTool` for in-process calls. The latter is ~50× lower latency.

---

## 2. Tell the agent when to use it

Agents default to `Grep` + `Read`. Unless the system prompt or project-level instructions direct otherwise, they won't prefer neargrep even when it would save 10× the tokens.

**Drop the snippet below into your project's `CLAUDE.md`, `AGENTS.md`, or equivalent.** Adapt tool names if your client uses a different prefix.

### Snippet — ready to paste

```markdown
## Code exploration

This repo is indexed by `neargrep` (MCP server registered in `.mcp.json`). When you need to understand unfamiliar code, prefer these tools over `Grep`/`Read`:

**First move for any code-understanding question: `mcp__neargrep__context`.**
Pass a natural-language query ("how does session refresh work", "where do we call Anthropic") or an identifier ("SessionManager"). Returns the top 5 matching symbols with full source, their callees, their callers, and file outlines for all files involved — typically enough to answer without any follow-up.

**Drill-down tools** (use only when `context` wasn't enough):
- `mcp__neargrep__search` — ranked symbols. Args: `query`, optional `k`, `mode={lexical,vector,hybrid}`, `kind={function,method,class,module,constant}`.
- `mcp__neargrep__expand` — call-graph neighborhood of a qname. Args: `qname`, `direction={callees,callers,both}`.
- `mcp__neargrep__outline` — nested symbol tree of a file. Args: `path`.
- `mcp__neargrep__source` — full body of one symbol. Args: `qname`, optional `with_neighbors`.

**Qnames** look like `module.path:Class.method` (colon separates module from member). If you already know a qname, pass it straight to `context` — it'll use a fast path (~1 ms).

**Fall back to `Grep` only for:**
- Searching string literals (URL patterns in `urls.py`, config JSON strings, log messages).
- TODO / FIXME comments.
- Filename patterns.

neargrep indexes **symbols** (functions, classes, methods, module-level / class-level constants). It does NOT index comments, docstrings of modules themselves, or raw string content inside function bodies.

**If `neargrep` returns a "no index" error:** run `neargrep index <repo-root>` once, then retry.

**If code changed during the session:** results may be stale. Re-run `neargrep index <repo-root>` (incremental — only changed files re-parsed).
```

### What this snippet accomplishes

- **Routes exploration to neargrep first.** Without this, agents reach for `Grep` by habit.
- **Tells the agent which tool for which job.** The `context`-first rule is the biggest single win; drill-down tools handle the ~10% of cases where one call isn't enough.
- **Sets expectations about what's NOT indexed.** Prevents the agent from issuing doomed queries (e.g. looking for URL route strings via neargrep).
- **Gives the agent a recovery path** when the index is missing or stale.

### Tuning per-project

If the agent keeps issuing searches for things that aren't symbols (route strings, env var names, etc.), add a project-specific line:

```markdown
**In this repo, use `Grep` for:** URL patterns in `urls/`, env vars starting with `APP_`, SQL migrations in `migrations/`.
```

---

## Verify the setup

```bash
# 1. Index the repo
neargrep index /path/to/repo

# 2. Smoke-test the MCP server directly
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"t","version":"0"}}}' | neargrep-mcp --root /path/to/repo --no-warm
```

You should see a JSON response with `"serverInfo": {"name": "neargrep", ...}`.

Inside Claude Code: run `/mcp`. You should see `neargrep` listed with all 5 tools.

---

## Troubleshooting

**`neargrep-mcp: command not found`** — the tool isn't on `PATH`. Run `uv tool install --editable .` from the neargrep repo, or reference it by absolute path in `.mcp.json`.

**Server starts but agent never calls it** — Claude Code is using cached tool selection. Restart the client. Also verify your `CLAUDE.md` / `AGENTS.md` actually points at `mcp__neargrep__*` (exact tool names).

**`no index` warning in logs** — run `neargrep index <root>` once before the server can answer queries. The server stays up without an index but every tool call returns an error.

**Queries feel slow (~280 ms)** — the server is cold-loading the embedding model. First query always pays this (unless started with pre-warm, which is the default). Subsequent queries run in 5–10 ms.

**Results look stale** — incremental re-index on demand: `neargrep index <root>`. Takes <500 ms for single-file changes.

**MCP server crashes / fails to start** — check the log file your client writes (Claude Code puts MCP server logs under `~/Library/Logs/Claude/` on macOS). Common cause: missing dependencies, especially ONNX runtime on fresh installs. A `uv pip install -e .` from the neargrep repo usually resolves it.
