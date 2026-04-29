import { wrapLanguageModel } from "ai"

// Drains the upstream SSE stream fully (wire-level streaming preserved so
// providers that reject `stream:false` for large max_tokens still work),
// then replays every chunk synchronously so the consumer sees one tick of
// events instead of typewriter.
export function bufferStreamMiddleware() {
  return {
    specificationVersion: "v3",
    wrapStream: async ({ doStream }) => {
      const result = await doStream()
      const { stream, ...rest } = result
      const chunks = []
      const reader = stream.getReader()
      try {
        while (true) {
          const { done, value } = await reader.read()
          if (done) break
          chunks.push(value)
        }
      } finally {
        reader.releaseLock?.()
      }
      const replay = new ReadableStream({
        start(controller) {
          for (const chunk of chunks) controller.enqueue(chunk)
          controller.close()
        },
      })
      return { stream: replay, ...rest }
    },
  }
}

export const wrapModel = (m) =>
  wrapLanguageModel({
    model: m,
    middleware: bufferStreamMiddleware(),
  })

const LANGUAGE_MODEL_METHODS = ["languageModel", "chat", "chatModel", "responses", "completion", "completionModel"]
const PASSTHROUGH_METHODS = [
  "textEmbeddingModel", "embedding", "embeddingModel",
  "imageModel", "image",
  "transcription", "transcriptionModel",
  "speech", "speechModel",
]

export function buildProvider(real) {
  const provider = (modelId, settings) => wrapModel(real(modelId, settings))
  for (const key of LANGUAGE_MODEL_METHODS) {
    const fn = real[key]
    if (typeof fn === "function") {
      provider[key] = (modelId, settings) => wrapModel(fn.call(real, modelId, settings))
    }
  }
  for (const key of PASSTHROUGH_METHODS) {
    const fn = real[key]
    if (typeof fn === "function") provider[key] = fn.bind(real)
  }
  return provider
}
