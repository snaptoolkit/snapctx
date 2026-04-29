import { createOpenAI } from "@ai-sdk/openai"
import { buildProvider } from "./_buffer.mjs"

export function createBufferedOpenAI(options) {
  return buildProvider(createOpenAI(options))
}
