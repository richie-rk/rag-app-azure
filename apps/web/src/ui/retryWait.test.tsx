import { act, renderHook } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";

import { clientError, type ApiError } from "../api/problem";
import { useRetryWaits } from "./retryWait";

function refused(status: number, retryAfterSeconds: number | null, receivedAt: number): ApiError {
  return { ...clientError("problem", { status }), retryAfterSeconds, receivedAt };
}

describe("waits after a refused write", () => {
  test("T-364 a 429 keeps its control waiting until the retry time passes", () => {
    vi.useFakeTimers();
    try {
      const { result } = renderHook(() => useRetryWaits());
      const now = Date.now();
      act(() => {
        result.current.waitFor("save", refused(429, 20, now));
        result.current.waitFor("other", refused(429, 5, now));
      });
      expect(result.current.isWaiting("save")).toBe(true);
      expect(result.current.isWaiting("other")).toBe(true);
      expect(result.current.isWaiting("unrelated")).toBe(false);
      act(() => {
        vi.advanceTimersByTime(5_000);
      });
      expect(result.current.isWaiting("other")).toBe(false);
      expect(result.current.isWaiting("save")).toBe(true);
      act(() => {
        vi.advanceTimersByTime(15_000);
      });
      expect(result.current.isWaiting("save")).toBe(false);
    } finally {
      vi.useRealTimers();
    }
  });

  test("only a 429 with retryAfterSeconds waits; a 503 doesn't", () => {
    const { result } = renderHook(() => useRetryWaits());
    act(() => {
      result.current.waitFor("a", refused(503, 5, Date.now()));
      result.current.waitFor("b", refused(429, null, Date.now()));
      result.current.waitFor("c", null);
    });
    expect(result.current.isWaiting("a")).toBe(false);
    expect(result.current.isWaiting("b")).toBe(false);
    expect(result.current.isWaiting("c")).toBe(false);
  });
});
