/**
 * Chat API client for the NDJSON streaming endpoint.
 * No hardcoded URLs. No function codes in frontend.
 */

import type { ChatRequest } from "./types";

const CHAT_API = import.meta.env.VITE_CHAT_API_URL || "";

export async function streamChat(
  request: ChatRequest,
  token: string,
): Promise<ReadableStream<string>> {
  const response = await fetch(`${CHAT_API}/chat`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify(request),
  });

  if (!response.ok) {
    throw new Error(`Chat request failed: ${response.statusText}`);
  }

  if (!response.body) {
    throw new Error("No response body");
  }

  return response.body.pipeThrough(new TextDecoderStream());
}
