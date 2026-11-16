// The NDJSON reader (10-web-client.md section 8.7). It splits the byte stream at 0x0A before
// decoding, so a line or a multi-byte character cut across network chunks is joined before it is
// decoded. An object whose `type` isn't one of the eight is skipped, so a newer API doesn't break
// an open page during a deploy; the 1,048,576-byte cap bounds memory.

import { MAX_STREAM_LINE_BYTES } from "../../platform/constants";
import { isStreamEventType, parseStreamEvent, type StreamEvent } from "./events";

export type ReaderFailureCode =
  "stream_unreadable" | "stream_incomplete" | "stream_stalled" | "stream_interrupted";

export type ReaderOutcome =
  /** A `done` or `error` event ended the turn; the reader was cancelled. */
  | { readonly kind: "terminal" }
  /** The user stopped the answer. */
  | { readonly kind: "stopped" }
  /** A sign-out stopped it; nothing is shown. */
  | { readonly kind: "aborted" }
  | { readonly kind: "failed"; readonly code: ReaderFailureCode };

/** What a rejected read means: the user's stop, the stall timer, a sign-out, or the connection. */
export type ReadRejection = "stopped" | "stalled" | "aborted" | "interrupted";

export type LineVerdict = "continue" | "terminal" | "unreadable";

export interface StreamSink {
  /** A complete, non-empty line arrived: the stall timer restarts. */
  lineArrived(): void;
  /** A validated event, handed to the state machine; whether the turn goes on. */
  event(event: StreamEvent): LineVerdict;
}

const NEWLINE = 0x0a;
const CARRIAGE_RETURN = 0x0d;

function concat(chunks: readonly Uint8Array[], last: Uint8Array): Uint8Array {
  if (chunks.length === 0) return last;
  const total = chunks.reduce((sum, chunk) => sum + chunk.byteLength, last.byteLength);
  const joined = new Uint8Array(total);
  let offset = 0;
  for (const chunk of [...chunks, last]) {
    joined.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return joined;
}

/** One complete line: section 8.7's `handleLine`. */
export function handleLine(line: Uint8Array, sink: StreamSink): LineVerdict {
  const end =
    line.byteLength > 0 && line[line.byteLength - 1] === CARRIAGE_RETURN
      ? line.byteLength - 1
      : line.byteLength;
  if (end === 0) return "continue";
  if (end > MAX_STREAM_LINE_BYTES) return "unreadable";
  sink.lineArrived();
  let text: string;
  try {
    text = new TextDecoder("utf-8", { fatal: true }).decode(line.subarray(0, end));
  } catch {
    return "unreadable";
  }
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch {
    return "unreadable";
  }
  if (typeof value !== "object" || value === null || Array.isArray(value)) return "unreadable";
  const type = (value as { type?: unknown }).type;
  if (typeof type !== "string") return "unreadable";
  if (!isStreamEventType(type)) return "continue";
  const event = parseStreamEvent(type, value);
  return event === null ? "unreadable" : sink.event(event);
}

const REJECTION_OUTCOMES: Record<ReadRejection, ReaderOutcome> = {
  stopped: { kind: "stopped" },
  aborted: { kind: "aborted" },
  stalled: { kind: "failed", code: "stream_stalled" },
  interrupted: { kind: "failed", code: "stream_interrupted" },
};

/** Reads `body` to its end or to the turn's terminal event. `classify` says what a rejected read
 * means, from the reason the request was aborted with. */
export async function readNdjson(
  body: ReadableStream<Uint8Array>,
  sink: StreamSink,
  classify: (reason: unknown) => ReadRejection,
): Promise<ReaderOutcome> {
  const reader = body.getReader();
  const cancel = () => {
    reader.cancel().catch(() => undefined);
  };
  let pending: Uint8Array[] = [];
  let pendingBytes = 0;
  for (;;) {
    let result: ReadableStreamReadResult<Uint8Array>;
    try {
      result = await reader.read();
    } catch (reason) {
      return REJECTION_OUTCOMES[classify(reason)];
    }
    if (result.done) break;
    const value = result.value;
    let start = 0;
    for (let index = value.indexOf(NEWLINE); index !== -1; index = value.indexOf(NEWLINE, start)) {
      const line = concat(pending, value.subarray(start, index));
      pending = [];
      pendingBytes = 0;
      start = index + 1;
      const verdict = handleLine(line, sink);
      if (verdict === "unreadable") {
        cancel();
        return { kind: "failed", code: "stream_unreadable" };
      }
      if (verdict === "terminal") {
        cancel();
        return { kind: "terminal" };
      }
    }
    if (start < value.byteLength) {
      // A copy, because the chunk's buffer belongs to the stream.
      const rest = value.slice(start);
      pending.push(rest);
      pendingBytes += rest.byteLength;
    }
    if (pendingBytes > MAX_STREAM_LINE_BYTES) {
      cancel();
      return { kind: "failed", code: "stream_unreadable" };
    }
  }
  // A final line without its newline, or a body that ends before `done` or `error`.
  return { kind: "failed", code: pendingBytes > 0 ? "stream_unreadable" : "stream_incomplete" };
}
