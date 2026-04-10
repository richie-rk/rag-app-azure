/**
 * NDJSON streaming chat hook.
 *
 * Replicates Max AI's useStreamChat behavior:
 *   - ReadableStream reader (not EventSource)
 *   - Parse NDJSON lines
 *   - First chunk = metadata + data_points
 *   - Middle chunks = content deltas
 *   - Final chunk = follow-up questions
 */

import { useCallback, useRef, useState } from "react";
import { streamChat } from "../api/chat";
import type { ChatMessage, ChatRequest, StreamChunk } from "../api/types";
import { useAuth } from "../auth/AuthProvider";

export function useStreamChat() {
  const { token } = useAuth();
  const [isStreaming, setIsStreaming] = useState(false);
  const [streamingContent, setStreamingContent] = useState("");
  const [dataPoints, setDataPoints] = useState<string[]>([]);
  const [followupQuestions, setFollowupQuestions] = useState<string[]>([]);
  const abortRef = useRef<AbortController | null>(null);

  const sendMessage = useCallback(
    async (request: ChatRequest): Promise<ChatMessage | null> => {
      if (!token) return null;

      setIsStreaming(true);
      setStreamingContent("");
      setDataPoints([]);
      setFollowupQuestions([]);

      let fullContent = "";

      try {
        const stream = await streamChat(request, token);
        const reader = stream.getReader();

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;

          // Parse NDJSON: split on newlines, parse each line
          const lines = value.split("\n").filter((l) => l.trim());
          for (const line of lines) {
            try {
              const chunk: StreamChunk = JSON.parse(line);
              const choice = chunk.choices?.[0];
              if (!choice) continue;

              // Content delta
              if (choice.delta?.content) {
                fullContent += choice.delta.content;
                setStreamingContent(fullContent);
              }

              // Metadata chunk (first chunk with data_points)
              if (choice.context?.data_points) {
                setDataPoints(choice.context.data_points);
              }

              // Follow-up questions (final chunk)
              if (choice.context?.followup_questions) {
                setFollowupQuestions(choice.context.followup_questions);
              }
            } catch {
              // Skip malformed lines
            }
          }
        }

        setIsStreaming(false);
        return {
          role: "assistant",
          content: fullContent,
          dataPoints: dataPoints,
          followupQuestions: [],
          timestamp: new Date().toISOString(),
        };
      } catch (err) {
        setIsStreaming(false);
        throw err;
      }
    },
    [token],
  );

  const stopStreaming = useCallback(() => {
    abortRef.current?.abort();
    setIsStreaming(false);
  }, []);

  return {
    sendMessage,
    stopStreaming,
    isStreaming,
    streamingContent,
    dataPoints,
    followupQuestions,
  };
}
