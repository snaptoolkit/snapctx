import { WRAPPERS, rewriteModelsForNostream } from "./nostream-shared.mjs"

async function NostreamGoogle() {
  return {
    provider: {
      id: "google",
      async models(provider) {
        return rewriteModelsForNostream(provider.models, WRAPPERS["google"])
      },
    },
  }
}

export default {
  id: "nostream-google",
  server: NostreamGoogle,
}
