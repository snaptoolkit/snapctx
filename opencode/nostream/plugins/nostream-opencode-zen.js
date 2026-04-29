import { WRAPPERS, rewriteModelsForNostream } from "./nostream-shared.mjs"

async function NostreamOpencodeZen() {
  return {
    provider: {
      id: "opencode",
      async models(provider) {
        return rewriteModelsForNostream(provider.models, WRAPPERS["openai-compatible"])
      },
    },
  }
}

export default {
  id: "nostream-opencode-zen",
  server: NostreamOpencodeZen,
}
