// `createChatTurn` (11-api.md section 3.3): the scripted NDJSON streams of 10-web-client.md section
// 13.2, written as the API frames them (one JSON object per line, `\n`-terminated, chunked), and
// the stored turn each outcome leaves behind, so a later `getSession` reads what a real API would
// have stored: `completed` after `done`, `failed` after `error`, `cancelled` when the client stops.

import type { IncomingMessage, ServerResponse } from "node:http";
import { setTimeout as sleep } from "node:timers/promises";

import type {
  ChatTurnRequest,
  StreamErrorEvent,
} from "../../../../../apps/web/src/api/generated/types.gen.ts";
import { defaultStream, hex32, uuid } from "./builders.ts";
import type { StoredSession, StoredTurn, StreamScript } from "./protocol.ts";
import { baseHeaders, sendProblem } from "./reply.ts";
import type { World } from "./world.ts";

/** Each stream error code's message: the text column of 11-api.md section 2.3. */
const ERROR_MESSAGES: Readonly<Record<StreamErrorEvent["code"], string>> = {
  conversation_unavailable: "Your conversation couldn't be loaded. Retry in a moment.",
  retrieval_unavailable: "The document search isn't responding. Retry in a moment.",
  source_screening_unavailable:
    "The safety check for sources isn't available right now. Retry in a moment.",
  prompt_too_large: "This conversation is too long to continue. Start a new chat.",
  question_filtered:
    "This question was blocked by the content safety filter. Rephrase it, then send it.",
  answer_filtered:
    "The answer was stopped by the content safety filter. Try asking in a different way.",
  model_busy: "The answer service is busy. Retry shortly.",
  model_unavailable: "The answer service isn't responding. Retry in a moment.",
  model_request_rejected:
    "The answer service couldn't process this request. Try a shorter question.",
  answer_incomplete: "The answer couldn't be completed. Retry or narrow the question.",
  turn_timeout: "The answer took too long. Retry the question.",
  turn_save_failed:
    "The answer was shown but couldn't be saved to your history. Resend the question if you need it saved.",
  service_restarting: "The service is restarting. Resend your question in a moment.",
  internal_error:
    "Something went wrong on our side. Retry, and quote the reference if it keeps happening.",
};

const HEARTBEAT_EVERY_MS = 2000;
const SLOW_STEP_MS = 400;
const LARGEST_LINE = 1_048_576;

/** The answer's deltas: one per sentence, never splitting a citation token. */
function deltasOf(answer: string): string[] {
  const parts = answer.split(/(?<=[.!?:]\s)/u).filter((part) => part.length > 0);
  return parts.length === 0 ? [] : parts;
}

function titleOf(question: string): string {
  const flat = question.replace(/\s+/g, " ").trim();
  return Array.from(flat).slice(0, 80).join("");
}

interface Outcome {
  status: StoredTurn["status"];
  answer: string;
  cited: boolean;
  errorCode: StoredTurn["errorCode"];
  followups: readonly string[];
}

export async function handleChatTurn(
  world: World,
  request: IncomingMessage,
  response: ServerResponse,
  body: ChatTurnRequest,
): Promise<void> {
  const project = world.projects.find((item) => item.projectId === body.projectId);
  const existing =
    body.sessionId === undefined || body.sessionId === null
      ? null
      : (world.sessions.find((item) => item.sessionId === body.sessionId) ?? null);
  if (body.sessionId !== undefined && body.sessionId !== null && existing === null) {
    sendProblem(request, response, "session_not_found", { sessionId: body.sessionId });
    return;
  }
  if (project === undefined) {
    sendProblem(request, response, "project_not_found");
    return;
  }
  if (!project.hasDefaultIndex) {
    sendProblem(request, response, "project_index_unavailable");
    return;
  }

  const script = { ...defaultStream(), ...world.takeStream() } satisfies StreamScript;
  const mode = script.mode ?? "normal";
  const now = new Date().toISOString();
  const sessionId = existing?.sessionId ?? uuid();
  const turnId = uuid();
  const citations = [...(script.citations ?? [])];
  const allowedLinks = [...(script.allowedLinks ?? [])];
  const stored: StoredTurn = {
    turnId,
    seq: (existing?.turns.length ?? 0) + 1,
    question: body.question,
    answer: "",
    answerTruncated: false,
    status: "streaming",
    errorCode: null,
    citations: [],
    allowedLinks: [],
    followups: [],
    feedback: null,
    createdAt: now,
    endedAt: null,
    ...(script.files === undefined ? {} : { files: script.files }),
  };
  const session: StoredSession = existing
    ? { ...existing, lastTurnAt: now, turns: [...existing.turns, stored] }
    : {
        sessionId,
        projectId: body.projectId,
        title: titleOf(body.question),
        createdAt: now,
        lastTurnAt: now,
        turns: [stored],
      };
  world.sessions = [session, ...world.sessions.filter((item) => item.sessionId !== sessionId)];

  const outcome: Outcome = {
    status: "streaming",
    answer: "",
    cited: false,
    errorCode: null,
    followups: [],
  };
  const store = () => {
    world.sessions = world.sessions.map((item) =>
      item.sessionId !== sessionId
        ? item
        : {
            ...item,
            turns: item.turns.map((current) =>
              current.turnId !== turnId
                ? current
                : {
                    ...current,
                    status: outcome.status,
                    answer: outcome.answer,
                    answerTruncated: outcome.status === "completed" && script.truncated === true,
                    errorCode: outcome.errorCode,
                    citations: outcome.cited ? citations : [],
                    allowedLinks: outcome.cited ? allowedLinks : [],
                    followups: outcome.status === "completed" ? [...outcome.followups] : [],
                    endedAt: outcome.status === "streaming" ? null : new Date().toISOString(),
                  },
            ),
          },
    );
  };

  // Read through an object: the close listener sets it while the stream is being written.
  const connection = { closed: false };
  const closed = (): boolean => connection.closed;
  let finished = false;
  const stopWaiting = new Set<() => void>();
  response.on("close", () => {
    connection.closed = true;
    for (const stop of stopWaiting) stop();
    if (!finished && outcome.status === "streaming") {
      // The client stopped or gave up: the API saves the turn `cancelled` (section 3.3.4).
      outcome.status = "cancelled";
      store();
    }
  });

  response.writeHead(200, { ...baseHeaders(request), "Content-Type": "application/x-ndjson" });
  response.socket?.setNoDelay(true);

  const write = async (text: string): Promise<void> => {
    if (closed()) return;
    if (mode !== "byteSplit") {
      response.write(text);
      return;
    }
    // One byte per write, so the client meets every line and multi-byte character cut apart.
    for (const byte of Buffer.from(text, "utf8")) {
      if (closed()) return;
      response.write(Buffer.from([byte]));
      await sleep(1);
    }
  };
  const send = async (event: Record<string, unknown>): Promise<void> => {
    await write(`${JSON.stringify(event)}\n`);
    if (mode === "slow" && !closed()) {
      await sleep(SLOW_STEP_MS);
      await write(`${JSON.stringify({ type: "heartbeat" })}\n`);
      await sleep(SLOW_STEP_MS);
    }
  };
  /** Waits for the spec's release (or the client's departure), with heartbeats unless stalled. */
  const holdHere = async (heartbeats: boolean): Promise<void> => {
    const held = world.hold();
    stopWaiting.add(held.cancel);
    const timer = heartbeats
      ? setInterval(() => {
          if (!closed()) response.write(`${JSON.stringify({ type: "heartbeat" })}\n`);
        }, HEARTBEAT_EVERY_MS)
      : null;
    await held.done;
    if (timer !== null) clearInterval(timer);
    stopWaiting.delete(held.cancel);
  };

  await send({
    type: "meta",
    sessionId,
    turnId,
    sessionCreated: existing === null,
    replayed: false,
    correlationId: hex32(),
  });
  if (script.hold === "afterMeta") await holdHere(true);

  switch (mode) {
    case "broadened":
      await send({ type: "status", status: "search_broadened" });
      if (script.hold === "afterStatus") await holdHere(true);
      else await sleep(800);
      break;
    case "unknownType":
      await send({ type: "sparkle", shine: true });
      break;
    case "oversized":
      await write(`${JSON.stringify({ type: "delta", text: "x".repeat(LARGEST_LINE + 16) })}\n`);
      finish(response, () => (finished = true));
      return;
    case "invalid":
      await send({ type: "delta" });
      finish(response, () => (finished = true));
      return;
    default:
      break;
  }

  for (const text of deltasOf(script.answer ?? "")) {
    await send({ type: "delta", text });
    outcome.answer += text;
    if (mode === "stalled") {
      // Nothing more arrives; the client's stall timer ends the turn.
      await holdHere(false);
      finish(response, () => (finished = true));
      return;
    }
  }
  if (script.hold === "beforeCitations") await holdHere(true);
  if (closed()) return;

  const failWith = async () => {
    const error = script.error ?? { code: "model_unavailable", retryable: true };
    await send({
      type: "error",
      code: error.code,
      message: ERROR_MESSAGES[error.code],
      correlationId: hex32(),
      retryable: error.retryable,
      retryAfterSeconds: error.retryAfterSeconds ?? null,
    });
    outcome.status = "failed";
    outcome.errorCode = error.code;
    store();
    finish(response, () => (finished = true));
  };

  if (mode === "error" && script.errorAfterCitations !== true) {
    await failWith();
    return;
  }
  await send({ type: "citations", citations, allowedLinks });
  outcome.cited = true;
  if (mode === "error") {
    await failWith();
    return;
  }
  if (mode === "missingTerminal") {
    // The turn stays `streaming` in storage, as it would after the API process died.
    store();
    finish(response, () => (finished = true));
    return;
  }
  const followups = script.followups ?? [];
  if (followups.length > 0) await send({ type: "followups", questions: followups });
  outcome.followups = followups;
  await send({
    type: "done",
    turnId,
    truncated: script.truncated === true,
    usage: {
      promptTokens: 1200,
      completionTokens: 80,
      cachedTokens: 0,
      reasoningTokens: 0,
      totalTokens: 1280,
      estimated: false,
    },
  });
  if (closed()) return;
  outcome.status = "completed";
  store();
  finish(response, () => (finished = true));
}

function finish(response: ServerResponse, mark: () => void): void {
  mark();
  if (!response.writableEnded) response.end();
}
