import { WRAPPERS, rewriteModelsForNostream } from "./nostream-shared.mjs"

async function NostreamAnthropic() {
  return {
    provider: {
      id: "anthropic",
      async models(provider) {
        return rewriteModelsForNostream(provider.models, WRAPPERS["anthropic"])
      },
    },
  }
}

export default {
  id: "nostream-anthropic",
  server: NostreamAnthropic,
}
