# opencode no-stream — buffered responses, no typewriter

Drop-in plugins + provider-SDK wrappers that change opencode's responses from "typewriter as the model generates" to "appears all at once when generation finishes." The wire-level call to the model provider stays streaming, so providers that reject `stream:false` for large `max_tokens` (notably opencode-zen) keep working — only the *consumer* sees the response as one block instead of tokens-as-they-arrive.

## Why this exists

opencode is built around streaming. `streamText` from the Vercel AI SDK is hardcoded in `session/llm.ts:333`, and the agent's processor (`session/processor.ts:548-554`) consumes the resulting event stream chunk-by-chunk. There's no config flag to turn that off. Forking opencode means rebasing on every release.

This integration sidesteps the source by hooking opencode's plugin API. Each plugin claims a provider id (`opencode`, `openai`, etc.) and replaces every model's `api.npm` with a path to a tiny wrapper that re-implements the upstream provider on top of `wrapLanguageModel({ middleware: bufferStreamMiddleware() })`. The middleware:

1. Calls upstream `doStream()` (HTTP-level streaming preserved).
2. Drains every chunk into an in-memory list.
3. Returns a fresh `ReadableStream` that replays all chunks synchronously.

Net effect: opencode receives what looks like an instant fully-formed stream. Tool calls, retries, abort — all unaffected.

## What's in this folder

```
opencode/nostream/
├── package.json              # pinned deps for the wrappers
├── plugins/                  # symlink/copy these into ~/.config/opencode/plugins/
│   ├── package.json          # marks plugins/ as ESM
│   ├── nostream-shared.mjs   # WRAPPERS map + model rewriter (path-portable)
│   ├── nostream-opencode-zen.js
│   ├── nostream-github-copilot.js
│   ├── nostream-openai.js
│   ├── nostream-anthropic.js
│   └── nostream-google.js
├── wrappers/                 # provider-SDK wrappers (resolved automatically)
│   ├── _buffer.mjs           # the middleware + buildProvider helper
│   ├── openai.mjs
│   ├── openai-compatible.mjs
│   ├── anthropic.mjs
│   └── google.mjs
└── README.md                 # this file
```

`nostream-shared.mjs` resolves paths relative to itself via `import.meta.url`, so symlinks work without absolute-path edits — clone the snapctx repo anywhere, symlink the plugins, you're done.

## Providers covered out of the box

| opencode `providerID` | Plugin | Wrapper SDK |
|---|---|---|
| `opencode` (zen gateway) | `nostream-opencode-zen.js` | `@ai-sdk/openai-compatible` |
| `openai` | `nostream-openai.js` | `@ai-sdk/openai` |
| `anthropic` | `nostream-anthropic.js` | `@ai-sdk/anthropic` |
| `google` | `nostream-google.js` | `@ai-sdk/google` |
| `github-copilot` | `nostream-github-copilot.js` | `@ai-sdk/openai` (preserves `chat()`/`responses()` so opencode's GPT-5+ dispatch keeps working) |

For LM Studio or any user-declared OpenAI-compatible provider, point its `npm` field directly at `wrappers/openai-compatible.mjs` from `opencode.json` — no plugin needed (see [LM Studio example](#lm-studio-and-other-user-declared-providers) below).

For other providers (`openrouter`, `cohere`, `mistral`, `groq`, `xai`, etc.), see [Adding a new provider](#adding-a-new-provider) — it's three small files.

## Prerequisites

* opencode installed and runnable. [Install instructions →](https://opencode.ai/docs/install)
* Node.js available (for `npm install` of the wrappers' deps). The wrappers themselves run in opencode's bundled runtime.

## Install

The opencode config root on macOS / Linux is `~/.config/opencode/`.

### 1. Install the wrappers' deps once

The wrappers import `ai` and `@ai-sdk/*` packages. Run this once after cloning snapctx:

```bash
cd ~/projects/snapctx/opencode/nostream
npm install
```

This creates `opencode/nostream/node_modules/` with the pinned AI SDK versions. It's gitignored.

### 2. Symlink the plugins into opencode's config

```bash
mkdir -p ~/.config/opencode/plugins

# One symlink per plugin you want. Symlink all of them, or just the
# ones whose providers you actually use — opencode auto-discovers
# whatever's there.
for f in nostream-shared.mjs \
         nostream-opencode-zen.js \
         nostream-github-copilot.js \
         nostream-openai.js \
         nostream-anthropic.js \
         nostream-google.js; do
  ln -s ~/projects/snapctx/opencode/nostream/plugins/$f \
        ~/.config/opencode/plugins/$f
done

# Also link the package.json so plugins/ is treated as ESM by Node:
ln -s ~/projects/snapctx/opencode/nostream/plugins/package.json \
      ~/.config/opencode/plugins/package.json
```

Restart opencode (or run `opencode run "..."` to pick up the new plugins). Each plugin logs `loading plugin` on startup.

### 3. (optional) LM Studio and other user-declared providers

If you have a custom provider in your `~/.config/opencode/opencode.json` (e.g. LM Studio, a self-hosted vLLM, a colleague's OpenAI-compatible gateway), the plugin path doesn't apply — those models flow through the `npm` field directly. Point the field at the matching wrapper:

```json
{
  "provider": {
    "lmstudio": {
      "npm": "file:///Users/you/projects/snapctx/opencode/nostream/wrappers/openai-compatible.mjs",
      "name": "LM Studio (local)",
      "options": { "baseURL": "http://localhost:1234/v1" },
      "models": {
        "qwen3.5-35b-a3b": { "name": "Qwen 3.5 35B" }
      }
    }
  }
}
```

The wrapper's `createBufferedOpenAICompatible` factory accepts the same options as `@ai-sdk/openai-compatible`'s `createOpenAICompatible`, so existing config keys (`baseURL`, `apiKey`, `headers`, `fetch`) pass through unchanged.

## Verify

```bash
opencode run "What is 2+2? One word answer."
tail -f ~/.local/share/opencode/log/$(ls -t ~/.local/share/opencode/log | head -1) | grep -E "loading plugin|loading local provider"
```

You should see lines like:

```
service=plugin path=file:///.../nostream-openai.js loading plugin
service=provider pkg=file:///.../nostream/wrappers/openai.mjs loading local provider
```

The first proves the plugin loaded; the second proves the wrapper is in the request path. The response itself should arrive as a single block instead of typewriter-style.

## Adding a new provider

The pattern is mechanical. To add e.g. `@ai-sdk/groq` for the `groq` provider:

1. Add the dep:
   ```bash
   cd ~/projects/snapctx/opencode/nostream
   npm install @ai-sdk/groq@<version-opencode-uses>
   ```
   Use the same version opencode bundles to avoid SDK API drift. Check `packages/opencode/package.json` in the opencode repo.

2. Add a wrapper file `wrappers/groq.mjs`:
   ```js
   import { createGroq } from "@ai-sdk/groq"
   import { buildProvider } from "./_buffer.mjs"
   export function createBufferedGroq(options) {
     return buildProvider(createGroq(options))
   }
   ```

3. Register in `plugins/nostream-shared.mjs`:
   ```js
   export const WRAPPERS = {
     // ...existing entries...
     groq: wrap("groq.mjs"),
   }
   ```

4. Add a plugin file `plugins/nostream-groq.js`:
   ```js
   import { WRAPPERS, rewriteModelsForNostream } from "./nostream-shared.mjs"

   async function NostreamGroq() {
     return {
       provider: {
         id: "groq",
         async models(provider) {
           return rewriteModelsForNostream(provider.models, WRAPPERS["groq"])
         },
       },
     }
   }

   export default { id: "nostream-groq", server: NostreamGroq }
   ```

5. Symlink it into `~/.config/opencode/plugins/`. Restart opencode.

Most upstream providers expose a `createXxx` factory and a `LanguageModelV3` that opencode reaches via `sdk.languageModel(id)`. If a provider is special — for instance, opencode dispatches via `sdk.responses(id)` and `sdk.chat(id)` for github-copilot — make sure your wrapper exposes those method names too. The shared `buildProvider` in `_buffer.mjs` already wraps `chat`, `chatModel`, `responses`, `completion`, `completionModel`, `languageModel` if they exist on the upstream — you usually don't need to do anything extra.

## How it works (deeper)

opencode resolves `model.api.npm` by either looking it up in `BUNDLED_PROVIDERS` (a hardcoded map in opencode's source) or by `await import(npm)` if the value is a `file://` URL. When the plugin rewrites `api.npm` to `file:///abs/path/to/wrappers/openai.mjs`, opencode dynamically imports that file and calls the first export starting with `create…` (`provider.ts:1521`). That returns a `LanguageModelV3`-shaped provider whose every model passes through `bufferStreamMiddleware`.

The middleware lives in `_buffer.mjs`. `wrapStream({ doStream })` calls upstream `doStream`, drains the resulting `ReadableStream`, then yields a fresh stream that emits all chunks at once. The Vercel AI SDK's `streamText` reads this fresh stream identically to the original — no consumer-side change.

Crucially, the *upstream* HTTP request still uses streaming — so opencode-zen's "Requests with `max_tokens > 4096` must have `stream=true`" gate doesn't trip. The bytes-on-the-wire pattern is identical to vanilla opencode; the only thing that changes is *when* the consumer sees them (one tick at the end vs. continuously).

For copilot specifically, opencode's auth flow injects a `fetch` callback via `provider.options.fetch` (in `plugin/github-copilot/copilot.ts`). That option flows through to our wrapper unmodified, so the OAuth-managed fetch is preserved — copilot tokens get refreshed exactly the same way.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Plugin file is in `~/.config/opencode/plugins/` but opencode says "Plugin export is not a function" on a `.mjs` file | opencode's plugin auto-discovery only scans `*.{ts,js}`, not `.mjs` | Make sure shared/helper files have `.mjs` extension and the plugin entry points have `.js`. The shipped files are correct — don't rename them. |
| "Cannot find module '@ai-sdk/openai' from /path/to/wrappers/openai.mjs" | `npm install` didn't run, or ran in the wrong dir | `cd opencode/nostream && npm install` |
| Provider still streams in the TUI even after symlinking | opencode wasn't restarted, OR the provider isn't covered | Restart opencode. Then check the log: if you see `pkg=@ai-sdk/<name> using bundled provider` instead of `pkg=file:///.../wrappers/<name>.mjs loading local provider`, the plugin for that provider isn't loaded. Add a plugin file for that providerID (see [Adding a new provider](#adding-a-new-provider)). |
| `Error from provider: Extra inputs are not permitted, field: '_upstreamNpm'` | An old version of the plugin shipped a per-model option that leaked into the API request | Pull the latest snapctx — the current shared module sets only `api.npm`, never `model.options`. |
| Token-bouncing / mid-response abort isn't detected as fast as before | The middleware only releases events at end-of-stream, so cancellation feedback waits for the upstream completion | Expected. If sub-second abort matters, this integration isn't right for your workflow. |

## Versioning

The wrappers are pinned to specific `@ai-sdk/*` versions in `opencode/nostream/package.json`. When opencode upgrades to a newer AI SDK, the bundled provider may add fields the older wrapped version doesn't recognize, or vice versa. To bump:

```bash
cd opencode/nostream
# Look at the current versions in upstream opencode's packages/opencode/package.json
# (https://github.com/anomalyco/opencode/blob/main/packages/opencode/package.json)
npm install @ai-sdk/openai@<new> @ai-sdk/openai-compatible@<new> ai@<new>  # etc.
```

The wrappers themselves are tiny (4 lines each besides `_buffer.mjs`), so SDK API drift usually shows up as a clear error at provider-instantiation time, not as silent corruption. If you see `TypeError: createXxx is not a function`, the upstream factory was renamed or removed — check the SDK's CHANGELOG.
