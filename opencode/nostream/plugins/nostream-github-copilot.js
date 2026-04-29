import { WRAPPERS, rewriteModelsForNostream } from "./nostream-shared.mjs"

async function NostreamGithubCopilot() {
  return {
    provider: {
      id: "github-copilot",
      async models(provider) {
        return rewriteModelsForNostream(provider.models, WRAPPERS["openai"])
      },
    },
  }
}

export default {
  id: "nostream-github-copilot",
  server: NostreamGithubCopilot,
}
