import { describe, expect, test } from "vitest";

import { clientError } from "../../api/problem";
import { liveTurn, storedTurn, turnIdOf } from "../../test/chat";
import { IDS } from "../../test/handlers";
import type { TurnFailure } from "../stream/turnStore";
import { arrangeTurns, newestSeq, rememberAnchor, turnModel } from "./turnModel";

function failure(fields: Partial<TurnFailure> = {}): TurnFailure {
  return {
    kind: "event",
    code: "model_unavailable",
    message: "The answer service isn't responding.",
    referenceId: "c".repeat(32),
    error: null,
    retryable: true,
    retryAt: null,
    blockedUntil: null,
    ...fields,
  };
}

describe("arranging stored and live turns", () => {
  test("stored turns are ordered by seq", () => {
    const { turns } = arrangeTurns([storedTurn({ seq: 2 }), storedTurn({ seq: 1 })], []);
    expect(turns.map((item) => item.key)).toEqual([turnIdOf(1), turnIdOf(2)]);
    expect(newestSeq([storedTurn({ seq: 2 }), storedTurn({ seq: 5 })])).toBe(5);
    expect(newestSeq([])).toBe(0);
  });

  test("T-375 a stored streaming copy of a live turn is ignored and the live one shows", () => {
    const live = liveTurn({ turnId: turnIdOf(2), phase: "streaming" });
    const stored = [storedTurn({ seq: 1 }), storedTurn({ seq: 2, status: "streaming" })];
    const { turns, replaced } = arrangeTurns(stored, [live]);
    expect(turns.map((item) => item.kind)).toEqual(["stored", "live"]);
    expect(replaced).toEqual([]);
  });

  test("a final stored turn replaces a settled live copy in place", () => {
    const live = liveTurn({ turnId: turnIdOf(2), phase: "done" });
    const { turns, replaced } = arrangeTurns(
      [storedTurn({ seq: 1 }), storedTurn({ seq: 2 })],
      [live],
    );
    expect(turns.map((item) => item.kind)).toEqual(["stored", "stored"]);
    expect(replaced).toEqual([turnIdOf(2)]);
  });

  test("a live turn before its outcome stays live even when stored is final", () => {
    for (const phase of ["streaming", "cited"] as const) {
      const live = liveTurn({ turnId: turnIdOf(1), phase });
      const { turns, replaced } = arrangeTurns([storedTurn({ seq: 1 })], [live]);
      expect(turns.map((item) => item.kind)).toEqual(["live"]);
      expect(replaced).toEqual([]);
    }
  });

  test("a failed turn that never reached the API stays above the turn that retried it", () => {
    const failed = liveTurn({
      clientTurnId: "11111111-1111-4111-8111-111111111111",
      turnId: null,
      phase: "failed",
      startedAt: 10,
    });
    rememberAnchor(failed.clientTurnId, 1);
    const stored = [storedTurn({ seq: 1 }), storedTurn({ seq: 2 })];
    const { turns } = arrangeTurns(stored, [failed]);
    expect(turns.map((item) => item.key)).toEqual([turnIdOf(1), failed.clientTurnId, turnIdOf(2)]);
  });

  test("a live turn without an anchor goes last, in send order", () => {
    const first = liveTurn({
      clientTurnId: "22222222-2222-4222-8222-222222222222",
      turnId: null,
      startedAt: 2,
    });
    const second = liveTurn({
      clientTurnId: "33333333-3333-4333-8333-333333333333",
      turnId: null,
      startedAt: 3,
    });
    const { turns } = arrangeTurns([storedTurn({ seq: 1 })], [second, first]);
    expect(turns.map((item) => item.key)).toEqual([
      turnIdOf(1),
      first.clientTurnId,
      second.clientTurnId,
    ]);
  });
});

describe("how a live turn looks", () => {
  const model = (fields: Parameters<typeof liveTurn>[0]) =>
    turnModel({ kind: "live", key: "k", turn: liveTurn(fields) }, null, null);

  test("the progress line shows until the first delta, broadened when the status arrived", () => {
    expect(model({ phase: "sending", text: "" }).progress).toBe("searching");
    expect(model({ phase: "streaming", text: "", broadened: true }).progress).toBe("broadened");
    expect(model({ phase: "streaming", text: "A" }).progress).toBeNull();
    expect(model({ phase: "failed", text: "", failure: failure() }).progress).toBeNull();
  });

  test("a done turn shows feedback, Copy answer and the truncated caption", () => {
    const done = model({ phase: "done", truncated: true });
    expect(done.outcome).toEqual({ kind: "truncated" });
    expect(done.feedback).toEqual({ stored: null });
    expect(done.copyable).toBe(true);
    expect(done.streaming).toBe(false);
    expect(model({ phase: "cited" }).feedback).toEqual({ stored: null });
  });

  test("a streaming turn has no actions yet", () => {
    const streaming = model({ phase: "streaming" });
    expect(streaming.streaming).toBe(true);
    expect(streaming.feedback).toBeNull();
    expect(streaming.copyable).toBe(false);
  });

  test("a stop keeps the partial answer, offers Retry, and feedback only with an answer and an ID", () => {
    const stopped = model({ phase: "stopped" });
    expect(stopped.outcome).toEqual({ kind: "stopped", stored: false });
    expect(stopped.retry).toEqual({ retryAt: null });
    expect(stopped.feedback).toEqual({ stored: null });
    expect(model({ phase: "stopped", text: "" }).feedback).toBeNull();
    expect(model({ phase: "stopped", turnId: null }).feedback).toBeNull();
  });

  test("T-369 a failure offers Retry only when retryable, from its time", () => {
    expect(model({ phase: "failed", failure: failure({ retryAt: 5_000 }) }).retry).toEqual({
      retryAt: 5_000,
    });
    expect(
      model({ phase: "failed", failure: failure({ code: "question_filtered", retryable: false }) })
        .retry,
    ).toBeNull();
    const pre = model({
      phase: "failed",
      turnId: null,
      failure: failure({ kind: "request", error: clientError("network"), code: null }),
    });
    expect(pre.outcome.kind).toBe("liveFailure");
    expect(pre.feedback).toBeNull();
    expect(pre.clientTurnId).toBe(IDS.clientTurn);
  });
});

describe("how a stored turn looks", () => {
  const model = (fields: Parameters<typeof storedTurn>[0]) =>
    turnModel({ kind: "stored", key: "k", turn: storedTurn(fields) }, IDS.session, IDS.projectA);

  test("a completed turn has feedback, Copy answer and follow-ups", () => {
    const completed = model({ followups: ["Next?"], feedback: null });
    expect(completed.outcome).toEqual({ kind: "none" });
    expect(completed.feedback).toEqual({ stored: null });
    expect(completed.followups).toEqual(["Next?"]);
    expect(completed.sessionId).toBe(IDS.session);
    expect(completed.projectId).toBe(IDS.projectA);
    expect(model({ answerTruncated: true }).outcome).toEqual({ kind: "truncated" });
  });

  test("a cancelled turn shows as stopped, with feedback only when it has an answer", () => {
    const cancelled = model({ status: "cancelled" });
    expect(cancelled.outcome).toEqual({ kind: "stopped", stored: true });
    expect(cancelled.retry).toEqual({ retryAt: null });
    expect(cancelled.followups).toBeNull();
    expect(model({ status: "cancelled", answer: "" }).feedback).toBeNull();
    expect(model({ status: "cancelled", answer: "" }).copyable).toBe(false);
  });

  test("T-376 a failed turn shows its error code, with Retry when the code is retryable", () => {
    expect(model({ status: "failed", errorCode: "turn_timeout" }).outcome).toEqual({
      kind: "storedFailure",
      code: "turn_timeout",
    });
    expect(model({ status: "failed", errorCode: "turn_timeout" }).retry).toEqual({ retryAt: null });
    expect(model({ status: "failed", errorCode: null }).retry).toEqual({ retryAt: null });
    for (const code of [
      "prompt_too_large",
      "question_filtered",
      "answer_filtered",
      "model_request_rejected",
    ] as const) {
      expect(model({ status: "failed", errorCode: code }).retry).toBeNull();
    }
    expect(model({ status: "failed" }).feedback).toBeNull();
  });

  test("a streaming turn is in progress, with nothing to copy", () => {
    const streaming = model({ status: "streaming" });
    expect(streaming.outcome).toEqual({ kind: "inProgress" });
    expect(streaming.copyable).toBe(false);
  });
});
