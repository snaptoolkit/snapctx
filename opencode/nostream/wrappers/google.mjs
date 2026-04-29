import { createGoogleGenerativeAI } from "@ai-sdk/google"
import { buildProvider } from "./_buffer.mjs"

export function createBufferedGoogle(options) {
  return buildProvider(createGoogleGenerativeAI(options))
}
