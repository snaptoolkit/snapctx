import { fileURLToPath, pathToFileURL } from "url"
import { dirname, join } from "path"

// Resolved relative to this file's *real* path so symlinks work without
// any absolute-path edits. opencode imports the plugin file via its
// symlink, Node follows to the target in the cloned snapctx repo, and
// `import.meta.url` here points to the repo's plugins/ dir — making
// the wrappers/ dir reachable at "../wrappers/<sdk>.mjs".
const here = dirname(fileURLToPath(import.meta.url))
const WRAPPERS_DIR = join(here, "..", "wrappers")
const wrap = (name) => pathToFileURL(join(WRAPPERS_DIR, name)).href

export const WRAPPERS = {
  openai: wrap("openai.mjs"),
  "openai-compatible": wrap("openai-compatible.mjs"),
  anthropic: wrap("anthropic.mjs"),
  google: wrap("google.mjs"),
}

export function rewriteModelsForNostream(models, wrapperUrl) {
  const out = {}
  for (const [id, model] of Object.entries(models ?? {})) {
    out[id] = {
      ...model,
      api: { ...model.api, npm: wrapperUrl },
    }
  }
  return out
}
