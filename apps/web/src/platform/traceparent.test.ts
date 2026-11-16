import { describe, expect, test } from "vitest";

import { newTraceparent, type RandomFill } from "./traceparent";

const FORMAT = /^00-[0-9a-f]{32}-[0-9a-f]{16}-01$/;

describe("traceparent", () => {
  test("T-357 every value has the W3C format with no all-zero ID", () => {
    for (let index = 0; index < 1_000; index += 1) {
      const value = newTraceparent();
      expect(value.header).toMatch(FORMAT);
      expect(value.traceId).not.toBe("0".repeat(32));
      expect(value.parentId).not.toBe("0".repeat(16));
      expect(value.header).toBe(`00-${value.traceId}-${value.parentId}-01`);
    }
  });

  test("T-357 10,000 generated values hold no duplicate trace ID", () => {
    const ids = new Set<string>();
    for (let index = 0; index < 10_000; index += 1) ids.add(newTraceparent().traceId);
    expect(ids.size).toBe(10_000);
  });

  test("T-357 an all-zero draw is drawn once more", () => {
    let calls = 0;
    const fill: RandomFill = (bytes) => {
      calls += 1;
      // The first trace-ID draw and the first parent-ID draw are all zero.
      bytes.fill(calls === 1 || calls === 3 ? 0 : calls);
    };
    const value = newTraceparent(fill);
    expect(calls).toBe(4);
    expect(value.traceId).toBe("02".repeat(16));
    expect(value.parentId).toBe("04".repeat(8));
  });
});
