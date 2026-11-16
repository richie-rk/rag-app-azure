import { describe, expect, test, vi } from "vitest";

import {
  ApiFailure,
  apiErrorOf,
  clientError,
  isProblem,
  retryAtOf,
  splitFieldErrors,
  type ApiError,
} from "./problem";

function unprocessable(errors: { path: string; reason: string }[]): ApiError {
  return { ...clientError("problem", { status: 422, code: "validation_failed" }), errors };
}

describe("problems", () => {
  test("T-361 a 422 entry at body.exampleQuestions.2 marks the third example question", () => {
    const split = splitFieldErrors(
      unprocessable([
        { path: "body.exampleQuestions.2", reason: "too_long" },
        { path: "body.name", reason: "required" },
        { path: "body.name", reason: "too_long" },
        { path: "query.limit", reason: "too_large" },
        { path: "path.projectId", reason: "invalid" },
        { path: "header.If-Match", reason: "malformed" },
        { path: "body.unknownField", reason: "unexpected" },
      ]),
      ["name", "description", "exampleQuestions.0", "exampleQuestions.1", "exampleQuestions.2"],
    );
    expect([...split.byField]).toEqual([
      ["exampleQuestions.2", "too_long"],
      ["name", "required"],
    ]);
    expect(split.unmatched.map((entry) => entry.path)).toEqual([
      "query.limit",
      "path.projectId",
      "header.If-Match",
      "body.unknownField",
    ]);
  });

  test("T-361 method settings are addressed by their dotted names", () => {
    const split = splitFieldErrors(
      unprocessable([{ path: "body.methodParameters.targetTokens", reason: "too_small" }]),
      ["methodParameters.targetTokens"],
    );
    expect(split.byField.get("methodParameters.targetTokens")).toBe("too_small");
  });

  test("a client error takes its trace ID as reference ID unless told otherwise", () => {
    vi.setSystemTime(1_000);
    const error = clientError("network", { operationId: "getCurrentUser", traceId: "t" });
    expect(error).toEqual({
      kind: "network",
      operationId: "getCurrentUser",
      status: null,
      code: null,
      title: null,
      detail: null,
      errors: [],
      retryAfterSeconds: null,
      traceId: "t",
      referenceId: "t",
      receivedAt: 1_000,
    });
    expect(clientError("network", { traceId: "t", referenceId: null }).referenceId).toBeNull();
    expect(clientError("aborted").referenceId).toBeNull();
    vi.useRealTimers();
  });

  test("an ApiFailure carries its error and names it", () => {
    const error = clientError("problem", { code: "admin_required", status: 403 });
    const failure = new ApiFailure(error);
    expect(failure.message).toBe("problem: admin_required");
    expect(new ApiFailure(clientError("network")).message).toBe("network");
    expect(apiErrorOf(failure)).toBe(error);
    expect(apiErrorOf(new Error("x"))).toBeNull();
  });

  test("isProblem matches status and, when given, code", () => {
    const error = clientError("problem", { status: 409, code: "index_busy" });
    expect(isProblem(error, 409)).toBe(true);
    expect(isProblem(error, 409, "index_busy")).toBe(true);
    expect(isProblem(error, 409, "other")).toBe(false);
    expect(isProblem(error, 404)).toBe(false);
    expect(isProblem(clientError("http", { status: 409 }), 409)).toBe(false);
    expect(isProblem(null, 409)).toBe(false);
  });

  test("the retry time counts from when the answer was classified", () => {
    const error = {
      ...clientError("problem", { status: 429 }),
      receivedAt: 10_000,
      retryAfterSeconds: 20,
    };
    expect(retryAtOf(error)).toBe(30_000);
    expect(retryAtOf(clientError("network"))).toBeNull();
  });
});
