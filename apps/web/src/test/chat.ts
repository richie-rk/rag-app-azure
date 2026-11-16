// Builders for the chat tests: stored turns and sessions as the API returns them, and live turns
// as the turn store holds them.

import type { Citation } from "../chat/stream/events";
import type { LiveTurn } from "../chat/stream/turnStore";
import type { StoredTurn } from "../chat/turns/turnModel";
import { IDS } from "./handlers";

export const CITATION: Citation = {
  number: 1,
  title: "Expenses guide",
  pageNumber: 3,
  headingPath: ["Claims"],
  contentType: "application/pdf",
  openAs: "pdf_page",
};

/** `value`, which the test expects to exist. */
export function present<T>(value: T | null | undefined): T {
  if (value === null || value === undefined) throw new Error("expected a value");
  return value;
}

/** A turn ID for each `seq`, so a session's turns don't collide. */
export function turnIdOf(seq: number): string {
  return `${String(seq).padStart(8, "0")}-0000-4000-8000-000000000000`;
}

export function storedTurn(fields: Partial<StoredTurn> = {}): StoredTurn {
  const seq = fields.seq ?? 1;
  return {
    turnId: turnIdOf(seq),
    seq,
    status: "completed",
    question: `Question ${String(seq)}`,
    answer: `Answer ${String(seq)} ⟦1⟧.`,
    answerTruncated: false,
    errorCode: null,
    citations: [CITATION],
    allowedLinks: [],
    followups: [],
    feedback: null,
    createdAt: "2026-11-05T10:00:00Z",
    endedAt: "2026-11-05T10:00:10Z",
    ...fields,
  };
}

export function sessionDetail(
  turns: readonly StoredTurn[] = [storedTurn()],
  fields: Record<string, unknown> = {},
) {
  return {
    sessionId: IDS.session,
    projectId: IDS.projectA,
    title: "Expense claims",
    createdAt: "2026-11-05T10:00:00Z",
    lastTurnAt: "2026-11-05T10:00:00Z",
    turnCount: turns.length,
    turns,
    earlierTurnsCursor: null,
    ...fields,
  };
}

export function sessionSummary(fields: Record<string, unknown> = {}) {
  return {
    sessionId: IDS.session,
    projectId: IDS.projectA,
    title: "Expense claims",
    createdAt: "2026-11-05T10:00:00Z",
    lastTurnAt: "2026-11-05T10:00:00Z",
    turnCount: 1,
    ...fields,
  };
}

export function liveTurn(fields: Partial<LiveTurn> = {}): LiveTurn {
  return {
    clientTurnId: IDS.clientTurn,
    projectId: IDS.projectA,
    sessionId: IDS.session,
    turnId: IDS.turn,
    question: "Live question",
    phase: "done",
    broadened: false,
    text: "Live answer ⟦1⟧.",
    citations: [CITATION],
    allowedLinks: [],
    followups: null,
    truncated: false,
    failure: null,
    traceId: "a".repeat(32),
    metaCorrelationId: "b".repeat(32),
    controller: new AbortController(),
    startedAt: 1_000,
    ...fields,
  };
}
