// The turns a conversation shows and how each one looks (10-web-client.md sections 8.9, 8.10,
// 8.12, 8.13 and 8.16): stored turns from `getSession` and Show earlier messages, and this tab's
// live turns, merged so that a stored turn with a final status replaces its live copy in place, a
// stored `streaming` copy of a turn shown live is ignored, and a failed turn stays above the turn
// that retried it.

import type { ResponseOf } from "../../api/operations";
import { turnErrorText, type TurnErrorCode } from "../../copy/turnErrorTexts";
import type { Citation } from "../stream/events";
import { blocksSending, isSettled, type LiveTurn, type TurnFailure } from "../stream/turnStore";

export type StoredTurn = ResponseOf<"getSession">["turns"][number];
export type StoredFeedback = NonNullable<StoredTurn["feedback"]>;

const FINAL: ReadonlySet<string> = new Set(["completed", "cancelled", "failed"]);

export type DisplayTurn =
  | { readonly kind: "stored"; readonly key: string; readonly turn: StoredTurn }
  | { readonly kind: "live"; readonly key: string; readonly turn: LiveTurn };

/** For each live turn, the newest stored `seq` of its conversation when it was sent: a live turn
 * that never reached the API is shown after that turn, so it stays above the turn that retried
 * it once that one is stored. Kept for the page's lifetime, like the live turns. */
const anchors = new Map<string, number>();

export function rememberAnchor(clientTurnId: string, seq: number): void {
  anchors.set(clientTurnId, seq);
}

/** The newest stored `seq`, or 0 when nothing is stored yet. */
export function newestSeq(stored: readonly StoredTurn[]): number {
  return stored.reduce((newest, turn) => Math.max(newest, turn.seq), 0);
}

export interface ArrangedTurns {
  readonly turns: readonly DisplayTurn[];
  /** Turn IDs whose live copy a final stored turn replaces: remove them from the turn store. */
  readonly replaced: readonly string[];
}

/** Merges stored and live turns into display order. */
export function arrangeTurns(
  stored: readonly StoredTurn[],
  live: readonly LiveTurn[],
): ArrangedTurns {
  const storedById = new Map(stored.map((turn) => [turn.turnId, turn]));
  const replaced: string[] = [];
  const shownLive = new Set<string>();
  const entries: { order: [number, number, number]; item: DisplayTurn }[] = [];
  for (const turn of live) {
    const copy = turn.turnId === null ? undefined : storedById.get(turn.turnId);
    // A turn still waiting for its follow-ups or `done` stays live until its outcome.
    if (copy !== undefined && FINAL.has(copy.status) && isSettled(turn)) {
      replaced.push(copy.turnId);
      continue;
    }
    if (copy !== undefined) shownLive.add(copy.turnId);
    const order: [number, number, number] =
      copy === undefined
        ? [anchors.get(turn.clientTurnId) ?? Number.MAX_SAFE_INTEGER, 1, turn.startedAt]
        : [copy.seq, 0, 0];
    entries.push({ order, item: { kind: "live", key: turn.turnId ?? turn.clientTurnId, turn } });
  }
  for (const turn of stored) {
    if (shownLive.has(turn.turnId)) continue;
    entries.push({ order: [turn.seq, 0, 0], item: { kind: "stored", key: turn.turnId, turn } });
  }
  entries.sort(
    (a, b) => a.order[0] - b.order[0] || a.order[1] - b.order[1] || a.order[2] - b.order[2],
  );
  return { turns: entries.map((entry) => entry.item), replaced };
}

export type TurnOutcome =
  | { readonly kind: "none" }
  | { readonly kind: "truncated" }
  | { readonly kind: "stopped"; readonly stored: boolean }
  | { readonly kind: "liveFailure"; readonly failure: TurnFailure }
  | { readonly kind: "storedFailure"; readonly code: TurnErrorCode | null }
  | { readonly kind: "inProgress" };

export interface RetryOffer {
  /** When Retry becomes enabled, if not at once. */
  readonly retryAt: number | null;
}

export interface TurnModel {
  readonly key: string;
  readonly kind: "stored" | "live";
  /** A live turn's idempotency key, which Retry hands back to the turn store. */
  readonly clientTurnId: string | null;
  readonly question: string;
  readonly text: string;
  readonly citations: readonly Citation[] | null;
  readonly allowedLinks: readonly string[] | null;
  readonly sessionId: string | null;
  readonly turnId: string | null;
  readonly projectId: string | null;
  /** The answer is still arriving: the pipeline runs at most every 100 ms. */
  readonly streaming: boolean;
  /** The progress line until the first `delta` (section 8.11). */
  readonly progress: "searching" | "broadened" | null;
  readonly outcome: TurnOutcome;
  /** Feedback controls show, prefilled from this; null when they don't (section 8.16). */
  readonly feedback: { readonly stored: StoredFeedback | null } | null;
  readonly copyable: boolean;
  /** Retry, when this turn is the session's newest (section 8.10). */
  readonly retry: RetryOffer | null;
  /** Follow-up suggestions, when this turn is the session's newest (section 8.15). */
  readonly followups: readonly string[] | null;
}

function liveModel(item: { key: string; turn: LiveTurn }): TurnModel {
  const { turn } = item;
  const streaming = blocksSending(turn);
  const complete = turn.phase === "cited" || turn.phase === "done";
  const hasAnswer = turn.text !== "";
  const identified = turn.turnId !== null && turn.sessionId !== null;
  let outcome: TurnOutcome = { kind: "none" };
  if (turn.phase === "done" && turn.truncated) outcome = { kind: "truncated" };
  else if (turn.phase === "stopped") outcome = { kind: "stopped", stored: false };
  else if (turn.phase === "failed" && turn.failure !== null) {
    outcome = { kind: "liveFailure", failure: turn.failure };
  }
  let retry: RetryOffer | null = null;
  if (turn.phase === "stopped") retry = { retryAt: null };
  else if (turn.failure?.retryable === true) retry = { retryAt: turn.failure.retryAt };
  return {
    key: item.key,
    kind: "live",
    clientTurnId: turn.clientTurnId,
    question: turn.question,
    text: turn.text,
    citations: turn.citations,
    allowedLinks: turn.allowedLinks,
    sessionId: turn.sessionId,
    turnId: turn.turnId,
    projectId: turn.projectId,
    streaming,
    progress: streaming && !hasAnswer ? (turn.broadened ? "broadened" : "searching") : null,
    outcome,
    feedback:
      identified && (complete || (turn.phase === "stopped" && hasAnswer)) ? { stored: null } : null,
    copyable: !streaming && hasAnswer,
    retry,
    followups: turn.followups,
  };
}

function storedModel(
  item: { key: string; turn: StoredTurn },
  sessionId: string | null,
  projectId: string | null,
): TurnModel {
  const { turn } = item;
  const hasAnswer = turn.answer !== "";
  let outcome: TurnOutcome = { kind: "none" };
  let retry: RetryOffer | null = null;
  switch (turn.status) {
    case "completed":
      if (turn.answerTruncated) outcome = { kind: "truncated" };
      break;
    case "cancelled":
      outcome = { kind: "stopped", stored: true };
      retry = { retryAt: null };
      break;
    case "failed":
      outcome = { kind: "storedFailure", code: turn.errorCode };
      if (turnErrorText(turn.errorCode)?.retryable !== false) {
        retry = { retryAt: null };
      }
      break;
    case "streaming":
      outcome = { kind: "inProgress" };
      break;
  }
  return {
    key: item.key,
    kind: "stored",
    clientTurnId: null,
    question: turn.question,
    text: turn.answer,
    citations: turn.citations,
    allowedLinks: turn.allowedLinks,
    sessionId,
    turnId: turn.turnId,
    projectId,
    streaming: false,
    progress: null,
    outcome,
    feedback:
      turn.status === "completed" || (turn.status === "cancelled" && hasAnswer)
        ? { stored: turn.feedback }
        : null,
    copyable: hasAnswer && turn.status !== "streaming",
    retry,
    followups: turn.status === "completed" ? turn.followups : null,
  };
}

/** How a displayed turn looks. A stored turn belongs to the open session and its project. */
export function turnModel(
  item: DisplayTurn,
  sessionId: string | null,
  projectId: string | null,
): TurnModel {
  return item.kind === "live" ? liveModel(item) : storedModel(item, sessionId, projectId);
}
