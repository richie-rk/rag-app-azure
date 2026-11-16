// Builders for chat stream events and streamed responses, for the stream and chat tests.

import { CORRELATION_ID, IDS } from "./handlers";

export const metaEvent = (fields: Partial<Record<string, unknown>> = {}) => ({
  type: "meta",
  sessionId: IDS.session,
  turnId: IDS.turn,
  sessionCreated: false,
  replayed: false,
  correlationId: CORRELATION_ID,
  ...fields,
});

export const statusEvent = () => ({ type: "status", status: "search_broadened" });

export const deltaEvent = (text: string) => ({ type: "delta", text });

export const heartbeatEvent = () => ({ type: "heartbeat" });

export const citationsEvent = (count = 1, allowedLinks: readonly string[] = []) => ({
  type: "citations",
  citations: Array.from({ length: count }, (_, index) => ({
    number: index + 1,
    title: `Source ${String(index + 1)}`,
    pageNumber: index + 1,
    headingPath: [],
    contentType: "application/pdf",
    openAs: "pdf_page",
  })),
  allowedLinks,
});

export const followupsEvent = (questions: readonly string[] = ["What next?"]) => ({
  type: "followups",
  questions,
});

export const doneEvent = (truncated = false) => ({
  type: "done",
  turnId: IDS.turn,
  truncated,
  usage: {
    promptTokens: 10,
    completionTokens: 5,
    reasoningTokens: 0,
    cachedTokens: 0,
    totalTokens: 15,
    estimated: false,
  },
});

export const errorEvent = (fields: Partial<Record<string, unknown>> = {}) => ({
  type: "error",
  code: "model_unavailable",
  message: "The answer service isn't available right now.",
  retryable: true,
  retryAfterSeconds: null,
  correlationId: "fedcba9876543210fedcba9876543210",
  ...fields,
});

const encoder = new TextEncoder();

/** One NDJSON line per event. */
export function ndjson(...events: readonly unknown[]): Uint8Array {
  return encoder.encode(events.map((event) => `${JSON.stringify(event)}\n`).join(""));
}

/** A body that yields `chunks` and closes. */
export function bodyOf(...chunks: readonly Uint8Array[]): ReadableStream<Uint8Array> {
  return new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(chunk);
      controller.close();
    },
  });
}

/** A body the test feeds by hand; aborting `signal` errors it, as a browser does with a fetch
 * body. */
export function controlledBody(signal: AbortSignal) {
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  const body = new ReadableStream<Uint8Array>({
    start(value) {
      controller = value;
    },
  });
  signal.addEventListener("abort", () => {
    try {
      controller.error(signal.reason);
    } catch {
      // Already closed.
    }
  });
  return {
    body,
    send: (...events: readonly unknown[]) => {
      controller.enqueue(ndjson(...events));
    },
    close: () => {
      controller.close();
    },
    fail: (reason: unknown) => {
      controller.error(reason);
    },
  };
}

/** A `200` streamed answer. */
export function streamResponse(
  body: ReadableStream<Uint8Array> | null,
  contentType = "application/x-ndjson",
) {
  return new Response(body, { status: 200, headers: { "Content-Type": contentType } });
}
