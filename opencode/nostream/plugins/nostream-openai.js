import { WRAPPERS, rewriteModelsForNostream } from "./nostream-shared.mjs"

async function NostreamOpenAI() {
  return {
    provider: {
      id: "openai",
      async models(provider) {
        return rewriteModelsForNostream(provider.models, WRAPPERS["openai"])
      },
    },
  }
}

export default {
  id: "nostream-openai",
  server: NostreamOpenAI,
}
