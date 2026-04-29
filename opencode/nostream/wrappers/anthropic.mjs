import { createAnthropic } from "@ai-sdk/anthropic"
import { buildProvider } from "./_buffer.mjs"

export function createBufferedAnthropic(options) {
  return buildProvider(createAnthropic(options))
}
