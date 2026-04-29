# snapctx ⇄ opencode integration

Drop-in config that wires every snapctx operation into [opencode](https://opencode.ai) as a native tool, plus a global `AGENTS.md` ruleset that biases the model toward `snapctx_*` over `grep`/`read`/`glob`/`edit`.

Result: in real opencode sessions, ~10× fewer tool calls / ~10× faster / ~10× fewer tokens for navigation, and 3–4× fewer calls / 4–8× faster for refactors. Numbers in the [snapctx README](../README.md#tool-benchmark).

## What's in this folder

```
opencode/
├── AGENTS.md                  # global rules — paste into ~/.config/opencode/AGENTS.md
├── tools/
│   ├── snapctx.ts             # 18 tool wrappers (all snapctx read + write ops)
│   └── _snapctx_writer.py     # Python bridge for ops not in the snapctx CLI
├── nostream/                  # OPTIONAL: kill the typewriter effect on responses
│   └── README.md              # see opencode/nostream/README.md for details
└── README.md                  # this file
```

The tool file uses opencode's filename-prefix convention: each export becomes `snapctx_<name>` (so `export const context = …` → tool name `snapctx_context`, `export const edit_symbol = …` → `snapctx_edit_symbol`, etc.).

## Optional: turn off the typewriter effect

`opencode/nostream/` is a separate, opt-in integration that makes assistant responses appear all at once instead of streaming token-by-token. Provider-SDK wrappers + plugins that hook opencode's plugin API — no fork, survives upgrades, covers the common providers (`opencode` zen, `openai`, `anthropic`, `google`, `github-copilot`, plus user-declared OpenAI-compatible providers). See [`opencode/nostream/README.md`](nostream/README.md) for install + how it works.

## Tools wired

All 18 work-doing snapctx ops are exposed:

**Read** (8): `snapctx_map`, `snapctx_context`, `snapctx_search`, `snapctx_outline`, `snapctx_source`, `snapctx_expand`, `snapctx_find`, `snapctx_grep`.

**Write** (10): `snapctx_edit_symbol`, `snapctx_insert_symbol`, `snapctx_edit_batch`, `snapctx_delete_symbol`, `snapctx_add_import`, `snapctx_remove_import`, `snapctx_create_file`, `snapctx_delete_file`, `snapctx_move_file`, `snapctx_rename_symbol`.

CLI-exposed ops (`map`, `context`, `search`, `outline`, `source`, `expand`, `find`, `grep`, `edit_symbol`, `insert_symbol`) shell out to the `snapctx` binary. Python-API-only ops (`edit_batch`, `delete_symbol`, `add_import`, `remove_import`, `create_file`, `delete_file`, `move_file`, `rename_symbol`) route through `_snapctx_writer.py`, a small Python bridge that takes JSON on stdin and dispatches to `snapctx.api.<op>`.

## Prerequisites

1. **opencode** installed and runnable (`opencode --version`). [Install instructions →](https://opencode.ai/docs/install)
2. **snapctx** installed and on PATH:
   ```bash
   pip install snapctx          # or: pipx install snapctx
   which snapctx                # confirms it's on PATH
   ```
   Or set `SNAPCTX_BIN` to the absolute path if you don't want it on PATH.
3. **Python 3.11+** with `snapctx` importable. Test with:
   ```bash
   python3 -c "from snapctx import api; print('ok')"
   ```
   If you keep snapctx in a venv, point `SNAPCTX_PYTHON` at that venv's `python` binary.

## Install

The opencode config root on macOS / Linux is `~/.config/opencode/`. Either symlink (recommended — pulls upstream changes via `git pull`) or copy.

### Option A — symlink (recommended)

```bash
git clone https://github.com/snaptoolkit/snapctx.git ~/projects/snapctx
mkdir -p ~/.config/opencode/tools

ln -s ~/projects/snapctx/opencode/AGENTS.md            ~/.config/opencode/AGENTS.md
ln -s ~/projects/snapctx/opencode/tools/snapctx.ts     ~/.config/opencode/tools/snapctx.ts
ln -s ~/projects/snapctx/opencode/tools/_snapctx_writer.py \
                                                        ~/.config/opencode/tools/_snapctx_writer.py
```

### Option B — copy

```bash
cp -r opencode/* ~/.config/opencode/
```

You'll need to re-copy when this folder updates.

### Verify

Open opencode in any indexed (or auto-indexable) repo and run:

```
opencode run "list available tools that start with snapctx_"
```

You should see all 18 tools enumerated.

## Configure (env vars, optional)

Defaults work for most setups. Override only if your install is non-standard:

| Env var | Purpose | Default |
|---|---|---|
| `SNAPCTX_BIN` | Absolute path to the `snapctx` CLI | `snapctx` (PATH lookup) |
| `SNAPCTX_PYTHON` | Python interpreter that has `snapctx` importable | `python3` (PATH lookup) |
| `SNAPCTX_BRIDGE` | Absolute path to `_snapctx_writer.py` | sibling of `snapctx.ts` |

Set them in your shell profile or in `~/.config/opencode/opencode.json` under `env`.

## How it works

### `AGENTS.md` precedence

opencode loads, in order: project `AGENTS.md` / `CLAUDE.md` (walking up from cwd) → `~/.config/opencode/AGENTS.md` → `~/.claude/CLAUDE.md`. **First match wins per category.** A project-level rules file overrides the global one — if a downstream repo has its own `AGENTS.md`, copy the relevant `snapctx_*` priority section into it (or the model will revert to grep/read habits there).

### Tool discovery

opencode auto-discovers TypeScript / JavaScript files under `~/.config/opencode/tools/`. Each named export becomes a tool, named `<filename_without_ext>_<exportname>` — so `tools/snapctx.ts` exporting `context` registers as `snapctx_context`. No registration step in `opencode.json` needed.

### Bridge script

Most snapctx write ops aren't on the CLI (they're in `snapctx.api`). The bridge script:

1. Reads `{"op": "<name>", "args": {...}, "root": "<cwd>"}` from stdin.
2. `from snapctx import api; api.<op>(root=root, **args)`.
3. Prints the result as JSON to stdout.

The TypeScript wrapper spawns `python3 _snapctx_writer.py`, pipes the JSON, and returns the stdout to opencode. That's it — ~30 lines on each side.

## Tweak it

### Adjust the priority rules

Edit `AGENTS.md` to taste. Common tweaks:

- **Soften "mandatory first move"** if the model is over-eager calling `snapctx_map` for trivial follow-ups: change "MUST" to "SHOULD" in the [Mandatory first move](AGENTS.md#mandatory-first-move) section.
- **Add project-specific aliases** ("when I say 'controller', I mean a Django view function") — append to the "Tool reference" / "Pick the right tool" section.
- **Disable the agent's built-ins entirely** — gate them in `~/.config/opencode/opencode.json`:
  ```json
  { "permission": { "grep": "deny", "read": "ask" } }
  ```
  Aggressive but bulletproof if the model still slips back to defaults after AGENTS.md tweaks.

### Add or modify tool wrappers

Each tool is a single `export const <name> = tool({...})` block in `tools/snapctx.ts`. To add a new wrapper:

```typescript
export const my_op = tool({
  description: "What the model sees and uses to decide whether to call this tool.",
  args: {
    foo: tool.schema.string().describe("Argument doc — model reads this."),
  },
  async execute(args, ctx) {
    // For CLI ops:
    return await run(["my-op", args.foo], ctx.directory)
    // ...or for Python-API ops:
    // return await callApi("my_op", args, ctx.directory)
  },
})
```

The description field is the model's only signal for *when* to call your tool — invest in it. Show task-shaped framing ("Use this when …", "Prefer over X for Y").

### Watch tool calls live

```bash
opencode run --print-logs --log-level DEBUG "<your prompt>" 2>&1 | grep snapctx_
```

Useful for tuning descriptions when the model misroutes — you can see exactly which tool it picked and why.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `snapctx_*` tools don't appear | files not in `~/.config/opencode/tools/` | re-symlink / re-copy; restart opencode |
| `snapctx not_installed` from a write op | `python3` doesn't have snapctx | `pip install snapctx`, or set `SNAPCTX_PYTHON` to a venv that does |
| `snapctx <op> exited 127` | `snapctx` CLI not on PATH | install snapctx, or set `SNAPCTX_BIN` to its absolute path |
| Model still uses `grep` / `read` despite AGENTS.md | project-level `AGENTS.md` / `CLAUDE.md` overrides global | add the priority section to the project's rules file |
| Tools return `{"error": "scope_unsupported"}` | passed a vendor scope to a write op | write ops don't operate on vendor packages — only on your own repo |

## Versioning

This folder is versioned alongside snapctx itself, so updates to the read/write surface arrive together with new `snapctx_*` tools. Pull this repo to refresh:

```bash
cd ~/projects/snapctx && git pull
# symlinks pick up the new files automatically
```

If you're on Option B (copy), re-run the `cp -r` command.
