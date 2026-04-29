import { createOpenAICompatible } from "@ai-sdk/openai-compatible"
import { buildProvider } from "./_buffer.mjs"

export function createBufferedOpenAICompatible(options) {
  return buildProvider(createOpenAICompatible(options))
}
