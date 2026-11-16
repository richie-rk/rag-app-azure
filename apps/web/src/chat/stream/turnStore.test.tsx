import { QueryClient } from "@tanstack/react-query";
import { renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import {
  afterEach,
  beforeEach,
  describe,
  expect,
  test,
  vi,
  type Mock,
  type MockInstance,
} from "vitest";

import { ApiFailure, clientError, type ApiError } from "../../api/problem";
import { registerLiveRegions } from "../../a11y/announce";
import type { ChatTurnRequest } from "../../api/generated/types.gen";
import { abortAllWork, hasActiveWork } from "../../platform/activeWork";
import { CORRELATION_ID, IDS } from "../../test/handlers";
import {
  citationsEvent,
  controlledBody,
  deltaEvent,
  doneEvent,
  errorEvent,
  followupsEvent,
  heartbeatEvent,
  metaEvent,
  statusEvent,
  streamResponse,
} from "../../test/streamEvents";
import {
  TurnStore,
  TurnStoreContext,
  blocksSending,
  isSettled,
  useConversationTurns,
  type TurnSpeech,
} from "./turnStore";

const TRACE = "a".repeat(32);

function problem(status: number, code: string, retryAfterSeconds: number | null = null): ApiError {
  return {
    ...clientError("problem", { operationId: "createChatTurn", status, code, traceId: TRACE }),
    title: "Refused",
    detail: "The request was refused.",
    retryAfterSeconds,
    referenceId: CORRELATION_ID,
  };
}

interface Harness {
  readonly store: TurnStore;
  readonly start: ReturnType<
    typeof vi.fn<
      (
        body: ChatTurnRequest,
        signal: AbortSignal,
      ) => Promise<{ response: Response; traceId: string }>
    >
  >;
  readonly invalidate: MockInstance<QueryClient["invalidateQueries"]>;
  readonly status: HTMLElement;
  readonly speech: {
    readonly feed: Mock<TurnSpeech["feed"]>;
    readonly flush: Mock<TurnSpeech["flush"]>;
  };
  readonly bodies: ReturnType<typeof controlledBody>[];
}

let unregisterRegions: () => void = () => undefined;

function harness(): Harness {
  const queryClient = new QueryClient();
  const invalidate = vi.spyOn(queryClient, "invalidateQueries");
  const bodies: ReturnType<typeof controlledBody>[] = [];
  const start = vi.fn((_: ChatTurnRequest, signal: AbortSignal) => {
    const body = controlledBody(signal);
    bodies.push(body);
    return Promise.resolve({ response: streamResponse(body.body), traceId: TRACE });
  });
  const speech = { feed: vi.fn<TurnSpeech["feed"]>(), flush: vi.fn<TurnSpeech["flush"]>() };
  let uuid = 0;
  const store = new TurnStore({
    queryClient,
    startChatTurn: start,
    speech,
    randomUUID: () => {
      uuid += 1;
      return `00000000-0000-4000-8000-${String(uuid).padStart(12, "0")}`;
    },
  });
  const log = document.createElement("div");
  const status = document.createElement("div");
  unregisterRegions = registerLiveRegions(log, status);
  return { store, start, invalidate, status, speech, bodies };
}

/** Lets the store's promise chain run. */
async function flush(): Promise<void> {
  for (let index = 0; index < 10; index += 1) await Promise.resolve();
}

const keysOf = (invalidate: Harness["invalidate"]) =>
  invalidate.mock.calls.map((call) => JSON.stringify((call[0] as { queryKey: unknown }).queryKey));

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  abortAllWork();
  unregisterRegions();
  vi.useRealTimers();
});

describe("live turns", () => {
  test("T-369 a sent question streams to done and sending is blocked until citations", async () => {
    const { store, start, bodies, invalidate, speech, status } = harness();
    const handle = store.send({ projectId: IDS.projectA, sessionId: null, question: "Why?" });
    const created = store.get(handle.clientTurnId);
    expect(created?.phase).toBe("sending");
    expect(created !== undefined && blocksSending(created)).toBe(true);
    await flush();
    expect(start.mock.calls[0]?.[0]).toEqual({
      projectId: IDS.projectA,
      clientTurnId: handle.clientTurnId,
      question: "Why?",
    });
    expect(store.get(handle.clientTurnId)?.phase).toBe("streaming");
    expect(store.get(handle.clientTurnId)?.traceId).toBe(TRACE);

    const body = bodies[0];
    if (body === undefined) throw new Error("no request");
    body.send(metaEvent({ sessionCreated: true }), deltaEvent("Because "), deltaEvent("so ⟦1⟧."));
    await flush();
    const answering = store.get(handle.clientTurnId);
    expect(answering?.sessionId).toBe(IDS.session);
    expect(answering?.turnId).toBe(IDS.turn);
    expect(answering?.metaCorrelationId).toBe(CORRELATION_ID);
    expect(answering?.text).toBe("Because so ⟦1⟧.");
    expect(answering !== undefined && blocksSending(answering)).toBe(true);
    expect(speech.feed).toHaveBeenCalledTimes(2);
    expect(keysOf(invalidate)).toContain(JSON.stringify(["sessions"]));

    body.send(citationsEvent(2, ["https://Docs.Example/a", "https://exa mple.test/"]));
    await flush();
    const cited = store.get(handle.clientTurnId);
    expect(cited?.phase).toBe("cited");
    expect(cited !== undefined && blocksSending(cited)).toBe(false);
    expect(cited?.citations).toHaveLength(2);
    expect(cited?.allowedLinks).toEqual(["https://docs.example/a"]);
    expect(speech.flush).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(50);
    expect(status.textContent).toBe("Answer complete. 2 sources.");

    body.send(followupsEvent(["Next?"]), doneEvent(true));
    await expect(handle.settled).resolves.toEqual({ kind: "settled", phase: "done" });
    const done = store.get(handle.clientTurnId);
    expect(done?.followups).toEqual(["Next?"]);
    expect(done?.truncated).toBe(true);
    expect(done !== undefined && isSettled(done)).toBe(true);
    expect(keysOf(invalidate)).toContain(JSON.stringify(["session", IDS.session]));
    expect(hasActiveWork()).toBe(false);
  });

  test("T-369 a broadened search sets the flag and is announced word for word", async () => {
    const { store, bodies, status } = harness();
    const handle = store.send({ projectId: IDS.projectA, sessionId: IDS.session, question: "Q" });
    await flush();
    bodies[0]?.send(metaEvent(), statusEvent());
    await flush();
    expect(store.get(handle.clientTurnId)?.broadened).toBe(true);
    await vi.advanceTimersByTimeAsync(50);
    expect(status.textContent).toBe("Searching more broadly for better matches");
  });

  test("meta tells the listeners which session a new chat got", async () => {
    const { store, bodies } = harness();
    const assigned = vi.fn();
    const unsubscribe = store.onSessionAssigned(assigned);
    store.send({ projectId: IDS.projectA, sessionId: null, question: "Q" });
    await flush();
    bodies[0]?.send(metaEvent());
    await flush();
    expect(assigned).toHaveBeenCalledWith(expect.objectContaining({ sessionId: IDS.session }));
    unsubscribe();
  });

  test("T-369 an error event fails the turn with its code, message and correlation ID", async () => {
    const { store, bodies, status } = harness();
    vi.setSystemTime(1_000_000);
    const handle = store.send({ projectId: IDS.projectA, sessionId: IDS.session, question: "Q" });
    await flush();
    bodies[0]?.send(
      metaEvent(),
      deltaEvent("partial"),
      errorEvent({ retryable: true, retryAfterSeconds: 30 }),
    );
    await expect(handle.settled).resolves.toEqual({ kind: "settled", phase: "failed" });
    const failed = store.get(handle.clientTurnId);
    expect(failed?.text).toBe("partial");
    expect(failed?.failure).toMatchObject({
      kind: "event",
      code: "model_unavailable",
      message: "The answer service isn't available right now.",
      referenceId: "fedcba9876543210fedcba9876543210",
      retryable: true,
      retryAt: 1_000_000 + 30_000,
    });
    await vi.advanceTimersByTimeAsync(50);
    expect(status.textContent).toBe("The answer couldn't be completed.");
  });

  test("T-369 no Retry for a filtered answer", async () => {
    const { store, bodies } = harness();
    const handle = store.send({ projectId: IDS.projectA, sessionId: IDS.session, question: "Q" });
    await flush();
    bodies[0]?.send(metaEvent(), errorEvent({ code: "answer_filtered", retryable: false }));
    await handle.settled;
    expect(store.get(handle.clientTurnId)?.failure).toMatchObject({
      retryable: false,
      retryAt: null,
    });
  });

  test("T-369 Stop keeps the partial answer, announces it and refreshes the session", async () => {
    const { store, bodies, invalidate, status } = harness();
    const handle = store.send({ projectId: IDS.projectA, sessionId: IDS.session, question: "Q" });
    await flush();
    bodies[0]?.send(metaEvent(), deltaEvent("half an answer"));
    await flush();
    store.stop(handle.clientTurnId);
    await expect(handle.settled).resolves.toEqual({ kind: "settled", phase: "stopped" });
    expect(store.get(handle.clientTurnId)?.text).toBe("half an answer");
    expect(store.get(handle.clientTurnId)?.failure).toBeNull();
    expect(keysOf(invalidate)).toEqual(
      expect.arrayContaining([
        JSON.stringify(["sessions"]),
        JSON.stringify(["session", IDS.session]),
      ]),
    );
    await vi.advanceTimersByTimeAsync(50);
    expect(status.textContent).toBe("Answer stopped.");
  });

  test("T-369 Stop isn't offered after citations", async () => {
    const { store, bodies } = harness();
    const handle = store.send({ projectId: IDS.projectA, sessionId: IDS.session, question: "Q" });
    await flush();
    bodies[0]?.send(metaEvent(), citationsEvent());
    await flush();
    store.stop(handle.clientTurnId);
    await flush();
    expect(store.get(handle.clientTurnId)?.phase).toBe("cited");
    bodies[0]?.send(doneEvent());
    await expect(handle.settled).resolves.toEqual({ kind: "settled", phase: "done" });
    store.stop("unknown");
  });

  test("a stop before the response arrives stops the turn", async () => {
    const { store, start } = harness();
    start.mockImplementationOnce(
      (_, signal) =>
        new Promise((_resolve, reject) => {
          signal.addEventListener("abort", () => {
            reject(new ApiFailure(clientError("aborted", { traceId: TRACE })));
          });
        }),
    );
    const handle = store.send({ projectId: IDS.projectA, sessionId: IDS.session, question: "Q" });
    await flush();
    store.stop(handle.clientTurnId);
    await expect(handle.settled).resolves.toEqual({ kind: "settled", phase: "stopped" });
  });

  test("T-368 45,000 ms without a complete line fails the turn as stalled", async () => {
    const { store, bodies } = harness();
    const handle = store.send({ projectId: IDS.projectA, sessionId: IDS.session, question: "Q" });
    await flush();
    bodies[0]?.send(metaEvent());
    await flush();
    await vi.advanceTimersByTimeAsync(44_999);
    expect(store.get(handle.clientTurnId)?.phase).toBe("streaming");
    await vi.advanceTimersByTimeAsync(1);
    await expect(handle.settled).resolves.toEqual({ kind: "settled", phase: "failed" });
    expect(store.get(handle.clientTurnId)?.failure).toMatchObject({
      kind: "stream",
      code: "stream_stalled",
      referenceId: CORRELATION_ID,
      retryable: true,
    });
  });

  test("T-368 heartbeats every 15 s keep a 3-minute stream alive", async () => {
    const { store, bodies } = harness();
    const handle = store.send({ projectId: IDS.projectA, sessionId: IDS.session, question: "Q" });
    await flush();
    bodies[0]?.send(metaEvent());
    for (let second = 15; second <= 180; second += 15) {
      await vi.advanceTimersByTimeAsync(15_000);
      bodies[0]?.send(heartbeatEvent());
    }
    await flush();
    expect(store.get(handle.clientTurnId)?.phase).toBe("streaming");
    bodies[0]?.send(citationsEvent(), doneEvent());
    await expect(handle.settled).resolves.toEqual({ kind: "settled", phase: "done" });
  });

  test("T-368 the stall timer also bounds the wait for the response headers", async () => {
    const { store, start } = harness();
    start.mockImplementationOnce(
      (_, signal) =>
        new Promise((_resolve, reject) => {
          signal.addEventListener("abort", () => {
            reject(new ApiFailure(clientError("aborted", { traceId: TRACE })));
          });
        }),
    );
    const handle = store.send({ projectId: IDS.projectA, sessionId: IDS.session, question: "Q" });
    await vi.advanceTimersByTimeAsync(45_000);
    await expect(handle.settled).resolves.toEqual({ kind: "settled", phase: "failed" });
    expect(store.get(handle.clientTurnId)?.failure).toMatchObject({
      code: "stream_stalled",
      referenceId: TRACE,
    });
  });

  test("a read that fails mid-stream is interrupted", async () => {
    const { store, bodies } = harness();
    const handle = store.send({ projectId: IDS.projectA, sessionId: IDS.session, question: "Q" });
    await flush();
    bodies[0]?.fail(new TypeError("network"));
    await handle.settled;
    expect(store.get(handle.clientTurnId)?.failure).toMatchObject({
      code: "stream_interrupted",
      referenceId: TRACE,
    });
  });

  test.each([
    [
      "a body that ends early",
      (body: ReturnType<typeof controlledBody>) => {
        body.send(metaEvent(), deltaEvent("x"));
        body.close();
      },
      "stream_incomplete",
    ],
    [
      "an invalid event",
      (body: ReturnType<typeof controlledBody>) => {
        body.send({ type: "delta", text: "" });
      },
      "stream_unreadable",
    ],
    [
      "an event out of order",
      (body: ReturnType<typeof controlledBody>) => {
        body.send(deltaEvent("x"));
      },
      "stream_unreadable",
    ],
  ])("T-369 %s fails the turn the reader's way", async (_, act, code) => {
    const { store, bodies } = harness();
    const handle = store.send({ projectId: IDS.projectA, sessionId: IDS.session, question: "Q" });
    await flush();
    const body = bodies[0];
    if (body === undefined) throw new Error("no request");
    act(body);
    await handle.settled;
    expect(store.get(handle.clientTurnId)?.failure).toMatchObject({
      kind: "stream",
      code,
      retryable: true,
    });
  });

  test("T-369 a 200 of another type is unreadable, and one without a body incomplete", async () => {
    const { store, start } = harness();
    start.mockResolvedValueOnce({
      response: streamResponse(null, "application/json"),
      traceId: TRACE,
    });
    const wrongType = store.send({
      projectId: IDS.projectA,
      sessionId: IDS.session,
      question: "Q",
    });
    await wrongType.settled;
    expect(store.get(wrongType.clientTurnId)?.failure?.code).toBe("stream_unreadable");

    start.mockResolvedValueOnce({ response: streamResponse(null), traceId: TRACE });
    const noBody = store.send({ projectId: IDS.projectA, sessionId: IDS.session, question: "Q" });
    await noBody.settled;
    expect(store.get(noBody.clientTurnId)?.failure?.code).toBe("stream_incomplete");
  });

  test("T-369 Retry sends the same question in the same session with a new clientTurnId", async () => {
    const { store, start, bodies } = harness();
    const first = store.send({
      projectId: IDS.projectA,
      sessionId: IDS.session,
      question: "Again?",
    });
    await flush();
    bodies[0]?.send(metaEvent(), errorEvent());
    await first.settled;
    const retried = store.retry(first.clientTurnId);
    expect(retried).not.toBeNull();
    expect(retried?.clientTurnId).not.toBe(first.clientTurnId);
    await flush();
    expect(start.mock.calls[1]?.[0]).toMatchObject({ sessionId: IDS.session, question: "Again?" });
    expect(store.getSnapshot().map((turn) => turn.clientTurnId)).toEqual([
      first.clientTurnId,
      retried?.clientTurnId,
    ]);
    expect(store.retry(retried?.clientTurnId ?? "")).toBeNull();
    expect(store.retry("unknown")).toBeNull();
  });

  test("T-371 409 turn_in_progress soon after the previous turn was cited is resent with the same clientTurnId", async () => {
    const { store, start, bodies } = harness();
    const first = store.send({ projectId: IDS.projectA, sessionId: IDS.session, question: "One" });
    await flush();
    bodies[0]?.send(metaEvent(), citationsEvent(), doneEvent());
    await first.settled;

    start.mockRejectedValueOnce(new ApiFailure(problem(409, "turn_in_progress", 2)));
    const second = store.send({ projectId: IDS.projectA, sessionId: IDS.session, question: "Two" });
    await flush();
    expect(start).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(999);
    expect(start).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(1);
    expect(start).toHaveBeenCalledTimes(3);
    expect(start.mock.calls[2]?.[0]).toEqual(start.mock.calls[1]?.[0]);
    bodies[1]?.send(metaEvent(), citationsEvent(), doneEvent());
    await expect(second.settled).resolves.toEqual({ kind: "settled", phase: "done" });
  });

  test("T-371 the resend happens at most twice, then the turn fails and sending waits", async () => {
    const { store, start, bodies } = harness();
    vi.setSystemTime(5_000_000);
    const first = store.send({ projectId: IDS.projectA, sessionId: IDS.session, question: "One" });
    await flush();
    bodies[0]?.send(metaEvent(), citationsEvent(), doneEvent());
    await first.settled;
    start.mockRejectedValue(new ApiFailure(problem(409, "turn_in_progress", 3)));
    const second = store.send({ projectId: IDS.projectA, sessionId: IDS.session, question: "Two" });
    await vi.advanceTimersByTimeAsync(2_000);
    await second.settled;
    expect(start).toHaveBeenCalledTimes(4);
    expect(store.get(second.clientTurnId)?.failure).toMatchObject({ retryable: false });
    expect(store.get(second.clientTurnId)?.failure?.blockedUntil).toBeGreaterThan(5_000_000);
  });

  test("409 turn_in_progress with no recent citations fails at once", async () => {
    const { store, start } = harness();
    start.mockRejectedValueOnce(new ApiFailure(problem(409, "turn_in_progress", 4)));
    const handle = store.send({ projectId: IDS.projectA, sessionId: null, question: "Q" });
    await handle.settled;
    expect(start).toHaveBeenCalledTimes(1);
    expect(store.get(handle.clientTurnId)?.failure).toMatchObject({
      code: "turn_in_progress",
      retryable: false,
    });
  });

  test("a stop during the resend wait stops the turn", async () => {
    const { store, start, bodies } = harness();
    const first = store.send({ projectId: IDS.projectA, sessionId: IDS.session, question: "One" });
    await flush();
    bodies[0]?.send(metaEvent(), citationsEvent(), doneEvent());
    await first.settled;
    start.mockRejectedValueOnce(new ApiFailure(problem(409, "turn_in_progress")));
    const second = store.send({ projectId: IDS.projectA, sessionId: IDS.session, question: "Two" });
    await flush();
    store.stop(second.clientTurnId);
    await expect(second.settled).resolves.toEqual({ kind: "settled", phase: "stopped" });
  });

  test("T-372 a 422 removes the live turn and hands the error back", async () => {
    const { store, start } = harness();
    const error = {
      ...problem(422, "validation_failed"),
      errors: [{ path: "body.question", reason: "too_long" }],
    };
    start.mockRejectedValueOnce(new ApiFailure(error));
    const handle = store.send({ projectId: IDS.projectA, sessionId: null, question: "Q" });
    await expect(handle.settled).resolves.toEqual({ kind: "rejected", error });
    expect(store.get(handle.clientTurnId)).toBeUndefined();
  });

  test.each([
    ["404 project_not_found", problem(404, "project_not_found"), { retryable: false }, true],
    ["404 session_not_found", problem(404, "session_not_found"), { retryable: false }, false],
    ["409 session_turn_limit", problem(409, "session_turn_limit"), { retryable: false }, false],
    [
      "409 project_index_unavailable",
      problem(409, "project_index_unavailable"),
      { retryable: false },
      true,
    ],
    [
      "409 client_turn_id_conflict",
      problem(409, "client_turn_id_conflict"),
      { retryable: false },
      false,
    ],
    ["409 turn_already_ended", problem(409, "turn_already_ended"), { retryable: true }, false],
    ["403 role_not_assigned", problem(403, "role_not_assigned"), { retryable: false }, false],
    ["500 internal_error", problem(500, "internal_error"), { retryable: true }, false],
    [
      "http",
      clientError("http", { status: 403, traceId: TRACE }),
      { retryable: true, referenceId: TRACE },
      false,
    ],
    ["network", clientError("network", { traceId: TRACE }), { retryable: true }, false],
    ["unexpected", clientError("unexpected", { traceId: TRACE }), { retryable: true }, false],
    [
      "session_expired",
      clientError("session_expired", { referenceId: null }),
      { retryable: false },
      false,
    ],
  ] as const)(
    "T-369 a pre-stream %s fails the turn as section 8.6 says",
    async (_, error, expected, refetchesProjects) => {
      const { store, start, invalidate } = harness();
      start.mockRejectedValueOnce(new ApiFailure(error));
      const handle = store.send({ projectId: IDS.projectA, sessionId: null, question: "Q" });
      await expect(handle.settled).resolves.toEqual({ kind: "settled", phase: "failed" });
      expect(store.get(handle.clientTurnId)?.failure).toMatchObject({
        kind: "request",
        error,
        ...expected,
      });
      expect(keysOf(invalidate).includes(JSON.stringify(["projects", "mine"]))).toBe(
        refetchesProjects,
      );
    },
  );

  test.each(["rate_limited", "too_many_streams", "token_quota_exceeded"])(
    "T-369 429 %s blocks sending and Retry until its time",
    async (code) => {
      const { store, start } = harness();
      start.mockRejectedValueOnce(new ApiFailure(problem(429, code, 20)));
      const handle = store.send({ projectId: IDS.projectA, sessionId: IDS.session, question: "Q" });
      await handle.settled;
      const failure = store.get(handle.clientTurnId)?.failure;
      expect(failure?.retryable).toBe(true);
      expect(failure?.retryAt).not.toBeNull();
      expect(failure?.blockedUntil).toBe(failure?.retryAt);
    },
  );

  test("T-369 a 503 enables Retry from its time", async () => {
    const { store, start } = harness();
    start.mockRejectedValueOnce(new ApiFailure(problem(503, "dependency_unavailable", 5)));
    const handle = store.send({ projectId: IDS.projectA, sessionId: IDS.session, question: "Q" });
    await handle.settled;
    const failure = store.get(handle.clientTurnId)?.failure;
    expect(failure).toMatchObject({ retryable: true, blockedUntil: null });
    expect(failure?.retryAt).not.toBeNull();
  });

  test("a rejection that isn't an API failure counts as network", async () => {
    const { store, start } = harness();
    start.mockRejectedValueOnce(new Error("bug"));
    const handle = store.send({ projectId: IDS.projectA, sessionId: IDS.session, question: "Q" });
    await handle.settled;
    expect(store.get(handle.clientTurnId)?.failure).toMatchObject({ code: null, retryable: true });
  });

  test("a sign-out aborts every live turn and shows nothing", async () => {
    const { store } = harness();
    const handle = store.send({ projectId: IDS.projectA, sessionId: IDS.session, question: "Q" });
    await flush();
    expect(hasActiveWork()).toBe(true);
    abortAllWork();
    await expect(handle.settled).resolves.toEqual({ kind: "aborted" });
    expect(hasActiveWork()).toBe(false);
  });

  test("clear aborts and forgets every turn", async () => {
    const { store } = harness();
    const handle = store.send({ projectId: IDS.projectA, sessionId: IDS.session, question: "Q" });
    await flush();
    store.clear();
    expect(store.getSnapshot()).toEqual([]);
    await expect(handle.settled).resolves.toEqual({ kind: "aborted" });
  });

  test("stored turns replace live ones by turnId, and a turn can be removed", async () => {
    const { store, bodies } = harness();
    const listener = vi.fn();
    const unsubscribe = store.subscribe(listener);
    const live = store.send({ projectId: IDS.projectA, sessionId: IDS.session, question: "Q" });
    const other = store.send({ projectId: IDS.projectA, sessionId: IDS.session, question: "R" });
    await flush();
    bodies[0]?.send(metaEvent(), citationsEvent(), doneEvent());
    await live.settled;
    store.removeByTurnIds(["unrelated"]);
    expect(store.getSnapshot()).toHaveLength(2);
    store.removeByTurnIds([IDS.turn]);
    expect(store.getSnapshot().map((turn) => turn.clientTurnId)).toEqual([other.clientTurnId]);
    store.remove("unknown");
    store.remove(other.clientTurnId);
    expect(store.getSnapshot()).toEqual([]);
    expect(listener).toHaveBeenCalled();
    unsubscribe();
  });

  test("a conversation's live turns: an open session's, or a new chat's in its project", async () => {
    const { store, bodies } = harness();
    store.send({ projectId: IDS.projectA, sessionId: null, question: "new A" });
    store.send({ projectId: IDS.projectB, sessionId: null, question: "new B" });
    store.send({ projectId: IDS.projectA, sessionId: IDS.session, question: "existing" });
    await flush();
    const wrapper = ({ children }: { children: ReactNode }) => (
      <TurnStoreContext.Provider value={store}>{children}</TurnStoreContext.Provider>
    );
    const newChat = renderHook(() => useConversationTurns(null, IDS.projectA), { wrapper });
    expect(newChat.result.current.map((turn) => turn.question)).toEqual(["new A"]);
    const session = renderHook(() => useConversationTurns(IDS.session, null), { wrapper });
    expect(session.result.current.map((turn) => turn.question)).toEqual(["existing"]);
    bodies.forEach((body) => {
      body.close();
    });
  });

  test("a turn removed while it streams runs on without updating anything", async () => {
    const { store, bodies, speech } = harness();
    const done = store.send({ projectId: IDS.projectA, sessionId: null, question: "Q" });
    await flush();
    store.remove(done.clientTurnId);
    bodies[0]?.send(metaEvent(), deltaEvent("x"), citationsEvent(), followupsEvent(), doneEvent());
    await expect(done.settled).resolves.toEqual({ kind: "settled", phase: "done" });
    expect(store.getSnapshot()).toEqual([]);
    expect(speech.feed).not.toHaveBeenCalled();

    const failed = store.send({ projectId: IDS.projectA, sessionId: null, question: "Q" });
    await flush();
    store.remove(failed.clientTurnId);
    bodies[1]?.close();
    await expect(failed.settled).resolves.toEqual({ kind: "settled", phase: "failed" });

    const stopped = store.send({ projectId: IDS.projectA, sessionId: null, question: "Q" });
    await flush();
    store.stop(stopped.clientTurnId);
    store.remove(stopped.clientTurnId);
    await expect(stopped.settled).resolves.toEqual({ kind: "settled", phase: "stopped" });
    expect(speech.flush).not.toHaveBeenCalled();
  });

  test("a stop that lands before the resend wait stops the turn at once", async () => {
    const { store, start, bodies } = harness();
    const first = store.send({ projectId: IDS.projectA, sessionId: IDS.session, question: "One" });
    await flush();
    bodies[0]?.send(metaEvent(), citationsEvent(), doneEvent());
    await first.settled;
    start.mockImplementationOnce((body) => {
      store.stop(body.clientTurnId);
      return Promise.reject(new ApiFailure(problem(409, "turn_in_progress")));
    });
    const second = store.send({ projectId: IDS.projectA, sessionId: IDS.session, question: "Two" });
    await expect(second.settled).resolves.toEqual({ kind: "settled", phase: "stopped" });
    expect(start).toHaveBeenCalledTimes(2);
  });

  test("409 turn_in_progress without a time blocks sending from now", async () => {
    const { store, start } = harness();
    vi.setSystemTime(2_000_000);
    start.mockRejectedValueOnce(new ApiFailure(problem(409, "turn_in_progress")));
    const handle = store.send({ projectId: IDS.projectA, sessionId: null, question: "Q" });
    await handle.settled;
    expect(store.get(handle.clientTurnId)?.failure?.blockedUntil).toBe(2_000_000);
  });

  test("an aborted request that nobody aborted counts as interrupted", async () => {
    const { store, start } = harness();
    start.mockRejectedValueOnce(new ApiFailure(clientError("aborted", { traceId: TRACE })));
    const handle = store.send({ projectId: IDS.projectA, sessionId: IDS.session, question: "Q" });
    await handle.settled;
    expect(store.get(handle.clientTurnId)?.failure).toMatchObject({
      code: "stream_interrupted",
      referenceId: TRACE,
    });
  });

  test("a 200 without a Content-Type is unreadable", async () => {
    const { store, start } = harness();
    start.mockResolvedValueOnce({ response: new Response(null, { status: 200 }), traceId: TRACE });
    const handle = store.send({ projectId: IDS.projectA, sessionId: IDS.session, question: "Q" });
    await handle.settled;
    expect(store.get(handle.clientTurnId)?.failure?.code).toBe("stream_unreadable");
  });

  test("a store without its own clock and IDs uses the browser's", () => {
    const store = new TurnStore({
      queryClient: new QueryClient(),
      startChatTurn: () => new Promise(() => undefined),
    });
    const handle = store.send({ projectId: IDS.projectA, sessionId: null, question: "Q" });
    expect(handle.clientTurnId).toMatch(/^[0-9a-f-]{36}$/);
    expect(store.get(handle.clientTurnId)?.startedAt).toBe(Date.now());
    store.clear();
  });

  test("a turn is settled only in done, failed or stopped", () => {
    const { store } = harness();
    const handle = store.send({ projectId: IDS.projectA, sessionId: null, question: "Q" });
    const turn = store.get(handle.clientTurnId);
    if (turn === undefined) throw new Error("no turn");
    const phases = ["sending", "streaming", "cited", "done", "failed", "stopped"] as const;
    expect(phases.map((phase) => isSettled({ ...turn, phase }))).toEqual([
      false,
      false,
      false,
      true,
      true,
      true,
    ]);
    expect(phases.map((phase) => blocksSending({ ...turn, phase }))).toEqual([
      true,
      true,
      false,
      false,
      false,
      false,
    ]);
  });

  test("the hooks refuse to run outside the signed-in app", () => {
    expect(() => renderHook(() => useConversationTurns(null, null))).toThrow("useTurnStore");
  });
});
