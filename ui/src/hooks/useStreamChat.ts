/**
 * NDJSON streaming chat hook.
 *
 * Reads the response as a ReadableStream (not EventSource) and parses one
 * JSON object per line: the first line is metadata + data_points, the last
 * line is follow-up questions, and the rest are content deltas.
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

      // Locals, so the returned message reflects THIS turn. The state
      // variables above exist for rendering; reading them here would give
      // the values captured when the callback was created (previous turn).
      let fullContent = "";
      let turnDataPoints: string[] = [];
      let turnFollowups: string[] = [];

      const controller = new AbortController();
      abortRef.current = controller;

      const handleLine = (line: string) => {
        let chunk: StreamChunk & { error?: string };
        try {
          chunk = JSON.parse(line);
        } catch {
          return; // genuinely malformed line
        }
        if (chunk.error) {
          throw new Error(chunk.error);
        }
        const choice = chunk.choices?.[0];
        if (!choice) return;

        if (choice.delta?.content) {
          fullContent += choice.delta.content;
          setStreamingContent(fullContent);
        }
        if (choice.context?.data_points) {
          turnDataPoints = choice.context.data_points;
          setDataPoints(turnDataPoints);
        }
        if (choice.context?.followup_questions) {
          turnFollowups = choice.context.followup_questions;
          setFollowupQuestions(turnFollowups);
        }
      };

      try {
        const stream = await streamChat(request, token, controller.signal);
        const reader = stream.getReader();

        // Carry-over buffer: network chunk boundaries are not line
        // boundaries, so a JSON object can arrive split across two reads.
        // Keep the trailing partial line and prepend it to the next read.
        let buffer = "";

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;

          buffer += value;
          const lines = buffer.split("\n");
          buffer = lines.pop() ?? ""; // last element is an incomplete tail
          for (const line of lines) {
            if (line.trim()) handleLine(line);
          }
        }
        // Flush a final line that arrived without a trailing newline.
        if (buffer.trim()) handleLine(buffer);

        setIsStreaming(false);
        return {
          role: "assistant",
          content: fullContent,
          dataPoints: turnDataPoints,
          followupQuestions: turnFollowups,
          timestamp: new Date().toISOString(),
        };
      } catch (err) {
        setIsStreaming(false);
        // Stop button: keep whatever streamed instead of surfacing an error.
        if (controller.signal.aborted) {
          if (!fullContent) return null;
          return {
            role: "assistant",
            content: fullContent,
            dataPoints: turnDataPoints,
            followupQuestions: [],
            timestamp: new Date().toISOString(),
          };
        }
        throw err;
      } finally {
        // Only clear our own controller: if a newer sendMessage overlapped,
        // abortRef already points at its controller and nulling it here
        // would break that turn's Stop button.
        if (abortRef.current === controller) {
          abortRef.current = null;
        }
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
