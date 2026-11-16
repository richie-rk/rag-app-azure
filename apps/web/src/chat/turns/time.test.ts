import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";

import { useWaiting } from "./time";

afterEach(() => {
  vi.useRealTimers();
});

describe("useWaiting", () => {
  test("is true until the time passes, then false", () => {
    vi.useFakeTimers();
    vi.setSystemTime(10_000);
    const initialProps: { until: number | null } = { until: 15_000 };
    const { result, rerender } = renderHook(({ until }) => useWaiting(until), { initialProps });
    expect(result.current).toBe(true);
    act(() => {
      vi.advanceTimersByTime(4_999);
    });
    expect(result.current).toBe(true);
    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(result.current).toBe(false);
    rerender({ until: null });
    expect(result.current).toBe(false);
  });
});
