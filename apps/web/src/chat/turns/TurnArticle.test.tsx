import { screen, within } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";

import { clientError, type ApiError } from "../../api/problem";
import { liveTurn, storedTurn } from "../../test/chat";
import { CURRENT_USER, IDS } from "../../test/handlers";
import { configureTestHttp, renderWithProviders } from "../../test/render";
import type { TurnFailure } from "../stream/turnStore";
import { TurnArticle, liveFailureText, type TurnArticleProps } from "./TurnArticle";
import { turnModel, type DisplayTurn } from "./turnModel";

function show(item: DisplayTurn, overrides: Partial<TurnArticleProps> = {}) {
  configureTestHttp();
  const props: TurnArticleProps = {
    model: turnModel(item, IDS.session, IDS.projectA),
    newest: true,
    sendingBlocked: false,
    onRetry: vi.fn(),
    onNewChat: vi.fn(),
    onCitation: vi.fn(),
    pollSession: vi.fn(() => Promise.resolve(true)),
    ...overrides,
  };
  const harness = renderWithProviders(<TurnArticle {...props} />, {
    currentUser: { ...CURRENT_USER, roles: ["User"] },
  });
  return { ...harness, props };
}

function failure(fields: Partial<TurnFailure>): TurnFailure {
  return {
    kind: "stream",
    code: "stream_incomplete",
    message: null,
    referenceId: "d".repeat(32),
    error: null,
    retryable: true,
    retryAt: null,
    blockedUntil: null,
    ...fields,
  };
}

function problem(status: number, code: string, fields: Partial<ApiError> = {}): ApiError {
  return {
    ...clientError("problem", { status, code, operationId: "createChatTurn" }),
    title: "Refused",
    detail: "The request was refused.",
    referenceId: "e".repeat(32),
    ...fields,
  };
}

describe("a turn", () => {
  test("is an article labelled by its question, with the answer, its sources and its actions", async () => {
    const { user, props } = show({ kind: "stored", key: "k", turn: storedTurn() });
    const article = screen.getByRole("article", { name: "Question 1" });
    expect(within(article).getByText("You asked")).toBeInTheDocument();
    expect(within(article).getByText("DocChat")).toBeInTheDocument();
    expect(within(article).getByRole("button", { name: "Helpful" })).toBeInTheDocument();
    expect(within(article).getByRole("button", { name: "Copy answer" })).toBeInTheDocument();
    await user.click(within(article).getByRole("button", { name: "Source 1: Expenses guide" }));
    await user.click(within(article).getByRole("button", { name: "Expenses guide, page 3" }));
    expect(props.onCitation).toHaveBeenCalledTimes(2);
  });

  test("T-374 shows the progress line until the first delta, broadened word for word", () => {
    show({
      kind: "live",
      key: "k",
      turn: liveTurn({ phase: "streaming", text: "", broadened: true, citations: null }),
    });
    expect(screen.getByText("Searching more broadly for better matches")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Copy answer" })).toBeNull();
  });

  test("a live stop keeps the partial answer with its caption and Retry on the newest turn", async () => {
    const { user, props } = show({
      kind: "live",
      key: "k",
      turn: liveTurn({ phase: "stopped", citations: null }),
    });
    expect(screen.getByText("You stopped this answer.")).toBeInTheDocument();
    expect(screen.getByText(/Live answer/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(props.onRetry).toHaveBeenCalledWith(props.model);
  });

  test("Retry shows only on the newest turn and waits while sending is blocked", () => {
    show(
      { kind: "stored", key: "k", turn: storedTurn({ status: "cancelled" }) },
      { sendingBlocked: true },
    );
    expect(screen.getByText("This answer was stopped before it finished.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry" })).toBeDisabled();
  });

  test("an older turn offers no Retry", () => {
    show(
      { kind: "stored", key: "k", turn: storedTurn({ status: "cancelled" }) },
      { newest: false },
    );
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
  });

  test("T-376 a stored failed turn shows its code's title and text, and no reference ID", () => {
    show({
      kind: "stored",
      key: "k",
      turn: storedTurn({ status: "failed", errorCode: "turn_timeout" }),
    });
    expect(screen.getByText("Answer took too long")).toBeInTheDocument();
    expect(screen.getByText("The answer took too long. Retry the question.")).toBeInTheDocument();
    expect(screen.queryByText(/Reference ID/)).toBeNull();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });

  test("a stored failed turn without a code shows the stored failure text", () => {
    show({ kind: "stored", key: "k", turn: storedTurn({ status: "failed", errorCode: null }) });
    expect(screen.getByText("This answer couldn't be completed.")).toBeInTheDocument();
  });

  test("a stored filtered turn offers no Retry", () => {
    show({
      kind: "stored",
      key: "k",
      turn: storedTurn({ status: "failed", errorCode: "question_filtered" }),
    });
    expect(screen.getByText("Question blocked")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
  });

  test("a truncated answer shows its caption", () => {
    show({ kind: "stored", key: "k", turn: storedTurn({ answerTruncated: true }) });
    expect(screen.getByText(/This answer reached its length limit/)).toBeInTheDocument();
  });

  test("a stored streaming turn shows the in-progress caption", () => {
    show({ kind: "stored", key: "k", turn: storedTurn({ status: "streaming", answer: "" }) });
    expect(screen.getByText("This answer is still being written.")).toBeInTheDocument();
    expect(screen.getByRole("switch", { name: "Auto-refresh" })).toBeInTheDocument();
  });

  test("an event failure shows its code's title, message and correlation ID", () => {
    show({
      kind: "live",
      key: "k",
      turn: liveTurn({
        phase: "failed",
        failure: failure({
          kind: "event",
          code: "model_busy",
          message: "The answer service is busy. Retry shortly.",
          retryAt: Date.now() + 30_000,
        }),
      }),
    });
    expect(screen.getByText("Answer service busy")).toBeInTheDocument();
    expect(screen.getByText("The answer service is busy. Retry shortly.")).toBeInTheDocument();
    expect(screen.getByText(`Reference ID: ${"d".repeat(32)}`)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry" })).toBeDisabled();
  });

  test("409 session_turn_limit offers New chat and no Retry", async () => {
    const { user, props } = show({
      kind: "live",
      key: "k",
      turn: liveTurn({
        phase: "failed",
        turnId: null,
        text: "",
        citations: null,
        failure: failure({
          kind: "request",
          error: problem(409, "session_turn_limit"),
          retryable: false,
        }),
      }),
    });
    expect(screen.getByText("Refused")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "New chat" }));
    expect(props.onNewChat).toHaveBeenCalled();
  });
});

describe("live failure texts", () => {
  test("for the reader's own failures, chat.error.title with the matching text", () => {
    expect(liveFailureText(failure({ code: "stream_incomplete" }))).toMatchObject({
      title: "The answer couldn't be completed",
      body: "The connection closed before the answer finished. Select Retry to send the question.",
    });
    expect(liveFailureText(failure({ code: "stream_stalled" }))?.body).toBe(
      "The answer stopped arriving. Select Retry to send the question.",
    );
    expect(liveFailureText(failure({ code: "stream_unreadable" }))?.body).toBe(
      "DocChat sent a reply this page couldn't read. Reload the page, then send the question.",
    );
    expect(liveFailureText(failure({ code: "stream_interrupted" }))?.body).toBe(
      "The connection was interrupted. Check your connection, then select Retry.",
    );
    expect(liveFailureText(failure({ code: null }))?.body).toBe(
      "The connection was interrupted. Check your connection, then select Retry.",
    );
  });

  test("for a pre-stream failure, the problem's text with the retry time, or the client's text", () => {
    const limited = liveFailureText(
      failure({ kind: "request", error: problem(429, "rate_limited", { retryAfterSeconds: 30 }) }),
    );
    expect(limited?.title).toBe("Refused");
    expect(limited?.lines[0]).toMatch(/^You can retry at /);
    expect(liveFailureText(failure({ kind: "request", error: clientError("network") }))).toEqual({
      title: "The answer couldn't be completed",
      body: "The connection was interrupted. Check your connection, then select Retry.",
      lines: [],
    });
    expect(
      liveFailureText(failure({ kind: "request", error: clientError("http", { status: 502 }) }))
        ?.title,
    ).toBe("Something went wrong");
    expect(liveFailureText(failure({ kind: "request", error: clientError("aborted") }))).toBeNull();
    expect(liveFailureText(failure({ kind: "request", error: null }))).toBeNull();
    expect(
      liveFailureText(failure({ kind: "event", code: "unknown_code", message: null })),
    ).toMatchObject({
      title: "The answer couldn't be completed",
      body: "",
    });
  });
});
