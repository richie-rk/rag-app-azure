// Live turns (10-web-client.md sections 8.6, 8.9 and 8.10): one external store, read through
// `useSyncExternalStore`, holding each turn started in this tab from Send to its terminal outcome.
// It sends `createChatTurn`, runs the stall timer and the NDJSON reader, applies each event and
// announces the outcomes. Streams outlive navigation: the store lives as long as the page.

import type { QueryClient } from "@tanstack/react-query";
import { createContext, useContext, useMemo, useSyncExternalStore } from "react";

import type { ChatStreamStart } from "../../api/http";
import type { ChatTurnRequest } from "../../api/generated/types.gen";
import { apiErrorOf, clientError, isProblem, retryAtOf, type ApiError } from "../../api/problem";
import { queryKeys } from "../../api/queryClient";
import { announceStatus } from "../../a11y/announce";
import { en } from "../../copy/en";
import { registerActiveWork } from "../../platform/activeWork";
import {
  STALL_TIMEOUT_MS,
  TURN_IN_PROGRESS_MAX_RESENDS,
  TURN_IN_PROGRESS_RESEND_AFTER_MS,
  TURN_IN_PROGRESS_WINDOW_MS,
} from "../../platform/constants";
import { pluralText } from "../../ui/format";
import {
  INITIAL_MACHINE,
  advance,
  isTerminal,
  normaliseAllowedLinks,
  type Citation,
  type Machine,
  type StreamEvent,
} from "./events";
import { readNdjson, type ReadRejection, type ReaderFailureCode } from "./ndjson";

/** `sending` until the `200`; `streaming` while awaiting `meta` and answering; `cited` unblocks
 * sending; then exactly one of `done`, `failed` or `stopped`. */
export type TurnPhase = "sending" | "streaming" | "cited" | "done" | "failed" | "stopped";

export interface TurnFailure {
  /** `event`: an `error` event; `stream`: a failure the reader detected; `request`: a pre-stream
   * failure (section 8.6). */
  readonly kind: "event" | "stream" | "request";
  /** The event's code, the reader's client outcome, or the problem's code. */
  readonly code: string | null;
  /** The `error` event's message. */
  readonly message: string | null;
  /** The event's `correlationId`; for the reader, `meta.correlationId` when `meta` arrived, else
   * the request's trace ID; for a request, section 5.5's. */
  readonly referenceId: string | null;
  /** The pre-stream failure itself, for its text. */
  readonly error: ApiError | null;
  /** Whether Retry is offered (section 8.10). */
  readonly retryable: boolean;
  /** When Retry is enabled, if not at once. */
  readonly retryAt: number | null;
  /** Until when sending, Retry, example cards and follow-ups stay disabled. */
  readonly blockedUntil: number | null;
}

export interface LiveTurn {
  /** The request's idempotency key. */
  readonly clientTurnId: string;
  readonly projectId: string;
  /** From the request, or from `meta` for a new session. */
  readonly sessionId: string | null;
  /** From `meta`. */
  readonly turnId: string | null;
  readonly question: string;
  readonly phase: TurnPhase;
  /** A `search_broadened` status arrived. */
  readonly broadened: boolean;
  /** The joined `delta` text. */
  readonly text: string;
  readonly citations: readonly Citation[] | null;
  /** From `citations`, normalised (section 9.3). */
  readonly allowedLinks: readonly string[] | null;
  readonly followups: readonly string[] | null;
  readonly truncated: boolean;
  readonly failure: TurnFailure | null;
  /** The request's trace ID; a resend updates it. */
  readonly traceId: string | null;
  readonly metaCorrelationId: string | null;
  /** Used by Stop, sign-out and the stall timer. */
  readonly controller: AbortController;
  /** Send time, in epoch milliseconds. */
  readonly startedAt: number;
}

/** Whether the turn still blocks sending in its session: before `citations` (section 8.5). */
export function blocksSending(turn: LiveTurn): boolean {
  return turn.phase === "sending" || turn.phase === "streaming";
}

/** Whether the turn has its terminal outcome. */
export function isSettled(turn: LiveTurn): boolean {
  return turn.phase === "done" || turn.phase === "failed" || turn.phase === "stopped";
}

/** Reads streamed text aloud (section 8.18); the answer renderer provides it. */
export interface TurnSpeech {
  /** After new text: speak what has reached a sentence boundary. */
  feed(turn: LiveTurn): void;
  /** Before the outcome's announcement: speak the rest. */
  flush(turn: LiveTurn): void;
}

export interface TurnStoreDependencies {
  readonly queryClient: QueryClient;
  /** `http.startChatTurn`. */
  readonly startChatTurn: (body: ChatTurnRequest, signal: AbortSignal) => Promise<ChatStreamStart>;
  readonly speech?: TurnSpeech;
  readonly now?: () => number;
  readonly randomUUID?: () => string;
}

export interface SendInput {
  readonly projectId: string;
  /** Null for a new session. */
  readonly sessionId: string | null;
  /** The text with leading and trailing white space removed. */
  readonly question: string;
}

export type Settlement =
  /** The turn reached `done`, `failed` or `stopped`. */
  | { readonly kind: "settled"; readonly phase: TurnPhase }
  /** A `422`: the live turn was removed; the question goes back to the composer with this error's
   * reason under it, and no Retry is offered. */
  | { readonly kind: "rejected"; readonly error: ApiError }
  /** A sign-out stopped it. */
  | { readonly kind: "aborted" };

export interface SendHandle {
  readonly clientTurnId: string;
  readonly settled: Promise<Settlement>;
}

/** The reasons a turn's request is aborted with, so a rejected read can tell them apart. */
const STOP = Symbol("stop");
const STALL = Symbol("stall");
const SIGN_OUT = Symbol("sign-out");

function rejectionOf(signal: AbortSignal): ReadRejection {
  if (!signal.aborted) return "interrupted";
  if (signal.reason === STOP) return "stopped";
  if (signal.reason === STALL) return "stalled";
  return "aborted";
}

/** Waits `ms`, or resolves false at once when `signal` aborts. */
function wait(ms: number, signal: AbortSignal): Promise<boolean> {
  return new Promise((resolve) => {
    if (signal.aborted) {
      resolve(false);
      return;
    }
    const onAbort = () => {
      clearTimeout(timer);
      resolve(false);
    };
    const timer = setTimeout(() => {
      signal.removeEventListener("abort", onAbort);
      resolve(true);
    }, ms);
    signal.addEventListener("abort", onAbort, { once: true });
  });
}

/** Retry is offered for these pre-stream answers (section 8.6), except where the table says no. */
function preStreamFailure(error: ApiError, now: number): TurnFailure {
  const base: TurnFailure = {
    kind: "request",
    code: error.code,
    message: null,
    referenceId: error.referenceId,
    error,
    retryable: true,
    retryAt: null,
    blockedUntil: null,
  };
  const retryAt = retryAtOf(error);
  if (error.kind === "session_expired") return { ...base, retryable: false };
  if (error.kind !== "problem") return base;
  if (
    isProblem(error, 403) ||
    isProblem(error, 404) ||
    isProblem(error, 409, "session_turn_limit")
  ) {
    return { ...base, retryable: false };
  }
  if (
    isProblem(error, 409, "project_index_unavailable") ||
    isProblem(error, 409, "client_turn_id_conflict")
  ) {
    return { ...base, retryable: false };
  }
  if (isProblem(error, 409, "turn_in_progress")) {
    return { ...base, retryable: false, blockedUntil: retryAt ?? now };
  }
  if (
    isProblem(error, 429, "rate_limited") ||
    isProblem(error, 429, "too_many_streams") ||
    isProblem(error, 429, "token_quota_exceeded")
  ) {
    return { ...base, retryAt, blockedUntil: retryAt };
  }
  if (isProblem(error, 503)) return { ...base, retryAt };
  return base;
}

export class TurnStore {
  private turns: readonly LiveTurn[] = [];
  private readonly listeners = new Set<() => void>();
  private readonly sessionListeners = new Set<(turn: LiveTurn) => void>();
  /** When this tab's last turn in each session reached `cited` (section 8.6, `409`). */
  private readonly citedAt = new Map<string, number>();
  private readonly now: () => number;
  private readonly randomUUID: () => string;

  constructor(private readonly deps: TurnStoreDependencies) {
    this.now = deps.now ?? Date.now;
    this.randomUUID = deps.randomUUID ?? (() => crypto.randomUUID());
  }

  readonly subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  /** Every live turn, oldest first; a new array after each change. */
  readonly getSnapshot = (): readonly LiveTurn[] => this.turns;

  get(clientTurnId: string): LiveTurn | undefined {
    return this.turns.find((turn) => turn.clientTurnId === clientTurnId);
  }

  /** Called when `meta` gives a turn its session, so a new chat still on screen can replace its
   * location with `/chat/<sessionId>` without remounting the screen. */
  onSessionAssigned(listener: (turn: LiveTurn) => void): () => void {
    this.sessionListeners.add(listener);
    return () => this.sessionListeners.delete(listener);
  }

  private emit(): void {
    for (const listener of this.listeners) listener();
  }

  private update(
    clientTurnId: string,
    change: (turn: LiveTurn) => Partial<LiveTurn>,
  ): LiveTurn | undefined {
    let updated: LiveTurn | undefined;
    this.turns = this.turns.map((turn) => {
      if (turn.clientTurnId !== clientTurnId) return turn;
      updated = { ...turn, ...change(turn) };
      return updated;
    });
    if (updated !== undefined) this.emit();
    return updated;
  }

  /** Sends a question (section 8.6). The live turn exists when this returns, so the composer can
   * clear its draft; `settled` resolves with the turn's outcome. */
  send(input: SendInput): SendHandle {
    const turn: LiveTurn = {
      clientTurnId: this.randomUUID(),
      projectId: input.projectId,
      sessionId: input.sessionId,
      turnId: null,
      question: input.question,
      phase: "sending",
      broadened: false,
      text: "",
      citations: null,
      allowedLinks: null,
      followups: null,
      truncated: false,
      failure: null,
      traceId: null,
      metaCorrelationId: null,
      controller: new AbortController(),
      startedAt: this.now(),
    };
    this.turns = [...this.turns, turn];
    this.emit();
    return { clientTurnId: turn.clientTurnId, settled: this.run(turn) };
  }

  /** Retry (section 8.10): the same question with a new `clientTurnId`, in the same session, or
   * as a new session when the turn never received `meta`. The failed turn stays. */
  retry(clientTurnId: string): SendHandle | null {
    const turn = this.get(clientTurnId);
    if (turn === undefined || !(turn.phase === "failed" || turn.phase === "stopped")) return null;
    return this.send({
      projectId: turn.projectId,
      sessionId: turn.sessionId,
      question: turn.question,
    });
  }

  /** Stop, by button or Escape: offered only before `citations`, because the answer is complete
   * from then on. */
  stop(clientTurnId: string): void {
    const turn = this.get(clientTurnId);
    if (turn !== undefined && blocksSending(turn)) turn.controller.abort(STOP);
  }

  /** Removes one live turn, such as after a `422` or once stored data replaces it. */
  remove(clientTurnId: string): void {
    const before = this.turns.length;
    this.turns = this.turns.filter((turn) => turn.clientTurnId !== clientTurnId);
    if (this.turns.length !== before) this.emit();
  }

  /** Removes the live turns whose `turnId` refetched session data holds with a final status, so
   * the stored turn replaces each in place (section 8.13). */
  removeByTurnIds(turnIds: Iterable<string>): void {
    const ids = new Set(turnIds);
    const before = this.turns.length;
    this.turns = this.turns.filter((turn) => turn.turnId === null || !ids.has(turn.turnId));
    if (this.turns.length !== before) this.emit();
  }

  /** Aborts every live turn and forgets them all, for a sign-out. */
  clear(): void {
    for (const turn of this.turns) turn.controller.abort(SIGN_OUT);
    this.turns = [];
    this.emit();
  }

  private async run(turn: LiveTurn): Promise<Settlement> {
    const id = turn.clientTurnId;
    const { signal } = turn.controller;
    let stallTimer: ReturnType<typeof setTimeout> | undefined;
    const restartStallTimer = () => {
      clearTimeout(stallTimer);
      stallTimer = setTimeout(() => {
        turn.controller.abort(STALL);
      }, STALL_TIMEOUT_MS);
    };
    const unregister = registerActiveWork({
      abort: () => {
        turn.controller.abort(SIGN_OUT);
      },
    });
    try {
      // The stall timer also bounds the wait for the response headers.
      restartStallTimer();
      const body: ChatTurnRequest = {
        projectId: turn.projectId,
        clientTurnId: id,
        question: turn.question,
        ...(turn.sessionId === null ? {} : { sessionId: turn.sessionId }),
      };
      let start: ChatStreamStart;
      for (let resends = 0; ; resends += 1) {
        try {
          start = await this.deps.startChatTurn(body, signal);
          break;
        } catch (reason) {
          const error =
            apiErrorOf(reason) ?? clientError("network", { operationId: "createChatTurn" });
          this.update(id, () => ({ traceId: error.traceId }));
          if (error.kind === "aborted") return this.interrupted(id, rejectionOf(signal));
          if (
            isProblem(error, 409, "turn_in_progress") &&
            resends < TURN_IN_PROGRESS_MAX_RESENDS &&
            this.citedRecently(turn.sessionId)
          ) {
            // The session's active turn is cleared before `citations` is written, and a replica
            // can lag behind it, so the identical request (same clientTurnId) goes once more.
            if (await wait(TURN_IN_PROGRESS_RESEND_AFTER_MS, signal)) continue;
            return this.interrupted(id, rejectionOf(signal));
          }
          return this.failBeforeStream(id, error);
        }
      }
      this.update(id, () => ({ traceId: start.traceId, phase: "streaming" }));
      const contentType = start.response.headers.get("Content-Type") ?? "";
      if (!contentType.toLowerCase().startsWith("application/x-ndjson")) {
        void start.response.body?.cancel().catch(() => undefined);
        return this.failStream(id, "stream_unreadable");
      }
      if (start.response.body === null) return this.failStream(id, "stream_incomplete");

      let machine: Machine = INITIAL_MACHINE;
      const outcome = await readNdjson(
        start.response.body,
        {
          lineArrived: restartStallTimer,
          event: (event) => {
            const next = advance(machine, event, turn.sessionId);
            if (next === null) return "unreadable";
            machine = next;
            this.apply(id, event);
            return isTerminal(machine.state) ? "terminal" : "continue";
          },
        },
        () => rejectionOf(signal),
      );
      if (outcome.kind === "terminal")
        return { kind: "settled", phase: this.get(id)?.phase ?? "done" };
      if (outcome.kind === "failed") return this.failStream(id, outcome.code);
      return this.interrupted(id, outcome.kind);
    } finally {
      clearTimeout(stallTimer);
      unregister();
    }
  }

  private citedRecently(sessionId: string | null): boolean {
    if (sessionId === null) return false;
    const at = this.citedAt.get(sessionId);
    return at !== undefined && this.now() - at < TURN_IN_PROGRESS_WINDOW_MS;
  }

  /** Applies one event the state machine allowed (the table of section 8.8). */
  private apply(id: string, event: StreamEvent): void {
    switch (event.type) {
      case "meta": {
        const turn = this.update(id, () => ({
          turnId: event.turnId,
          sessionId: event.sessionId,
          metaCorrelationId: event.correlationId,
        }));
        if (event.sessionCreated)
          void this.deps.queryClient.invalidateQueries({ queryKey: queryKeys.sessions });
        if (turn !== undefined) for (const listener of this.sessionListeners) listener(turn);
        return;
      }
      case "heartbeat":
        return;
      case "status":
        this.update(id, () => ({ broadened: true }));
        announceStatus(en["chat.turn.broadened"]);
        return;
      case "delta": {
        const turn = this.update(id, (current) => ({ text: current.text + event.text }));
        if (turn !== undefined) this.deps.speech?.feed(turn);
        return;
      }
      case "citations": {
        const turn = this.update(id, () => ({
          citations: event.citations,
          allowedLinks: normaliseAllowedLinks(event.allowedLinks),
          phase: "cited",
        }));
        if (turn?.sessionId != null) this.citedAt.set(turn.sessionId, this.now());
        if (turn !== undefined) this.deps.speech?.flush(turn);
        announceStatus(pluralText(en["chat.announce.done"], event.citations.length));
        return;
      }
      case "followups":
        this.update(id, () => ({ followups: event.questions }));
        return;
      case "done": {
        // `usage` passed its generated schema with the event and is never shown.
        const turn = this.update(id, () => ({ truncated: event.truncated, phase: "done" }));
        this.invalidateSession(turn);
        return;
      }
      case "error": {
        const retryAt =
          event.retryAfterSeconds === null ? null : this.now() + event.retryAfterSeconds * 1000;
        this.fail(id, {
          kind: "event",
          code: event.code,
          message: event.message,
          referenceId: event.correlationId,
          error: null,
          retryable: event.retryable,
          retryAt,
          blockedUntil: null,
        });
        return;
      }
    }
  }

  private invalidateSession(turn: LiveTurn | undefined): void {
    const { queryClient } = this.deps;
    void queryClient.invalidateQueries({ queryKey: queryKeys.sessions });
    if (turn?.sessionId != null) {
      void queryClient.invalidateQueries({ queryKey: queryKeys.session(turn.sessionId) });
    }
  }

  private fail(id: string, failure: TurnFailure): Settlement {
    const turn = this.update(id, () => ({ phase: "failed", failure }));
    if (turn !== undefined) this.deps.speech?.flush(turn);
    announceStatus(en["chat.announce.failed"]);
    this.invalidateSession(turn);
    return { kind: "settled", phase: "failed" };
  }

  private failStream(id: string, code: ReaderFailureCode): Settlement {
    const turn = this.get(id);
    return this.fail(id, {
      kind: "stream",
      code,
      message: null,
      referenceId: turn?.metaCorrelationId ?? turn?.traceId ?? null,
      error: null,
      retryable: true,
      retryAt: null,
      blockedUntil: null,
    });
  }

  private failBeforeStream(id: string, error: ApiError): Settlement {
    if (error.kind === "problem" && error.status === 422) {
      this.remove(id);
      return { kind: "rejected", error };
    }
    if (
      isProblem(error, 404, "project_not_found") ||
      isProblem(error, 409, "project_index_unavailable")
    ) {
      void this.deps.queryClient.invalidateQueries({ queryKey: queryKeys.myProjects });
    }
    return this.fail(id, preStreamFailure(error, this.now()));
  }

  /** A stop keeps the partial answer and any sources; a sign-out shows nothing. */
  private interrupted(id: string, rejection: ReadRejection): Settlement {
    if (rejection === "stalled") return this.failStream(id, "stream_stalled");
    if (rejection === "interrupted") return this.failStream(id, "stream_interrupted");
    if (rejection === "aborted") return { kind: "aborted" };
    const turn = this.update(id, () => ({ phase: "stopped" }));
    if (turn !== undefined) this.deps.speech?.flush(turn);
    announceStatus(en["chat.announce.stopped"]);
    this.invalidateSession(turn);
    return { kind: "settled", phase: "stopped" };
  }
}

export const TurnStoreContext = createContext<TurnStore | null>(null);

/** The page's turn store; inside the signed-in app. */
export function useTurnStore(): TurnStore {
  const store = useContext(TurnStoreContext);
  if (store === null) throw new Error("useTurnStore outside the signed-in app");
  return store;
}

/** Every live turn, re-rendering on each change. */
export function useLiveTurns(): readonly LiveTurn[] {
  const store = useTurnStore();
  return useSyncExternalStore(store.subscribe, store.getSnapshot);
}

/** The live turns of one conversation: an open session's, or a new chat's in a project (turns
 * that have no session yet). */
export function useConversationTurns(
  sessionId: string | null,
  projectId: string | null,
): readonly LiveTurn[] {
  const turns = useLiveTurns();
  return useMemo(
    () =>
      turns.filter((turn) =>
        sessionId === null
          ? turn.sessionId === null && turn.projectId === projectId
          : turn.sessionId === sessionId,
      ),
    [turns, sessionId, projectId],
  );
}
