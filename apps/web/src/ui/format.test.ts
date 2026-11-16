import { afterEach, describe, expect, test, vi } from "vitest";

import { en } from "../copy/en";
import {
  codePointLength,
  fillText,
  formatDate,
  formatDateTime,
  formatLastActivity,
  formatPercent,
  formatRetryTime,
  formatSize,
  formatTime,
  pluralText,
} from "./format";

afterEach(() => {
  vi.useRealTimers();
});

const NOW = new Date(2026, 10, 3, 10, 0, 0).getTime();

describe("formatters", () => {
  test("T-365 a retry time 10 s away shows seconds", () => {
    const at = NOW + 10_000;
    expect(formatRetryTime(at, NOW)).toBe(
      new Intl.DateTimeFormat(undefined, { timeStyle: "medium" }).format(at),
    );
  });

  test("T-365 a retry time 2 hours away and still today shows a short time", () => {
    const at = NOW + 2 * 3_600_000;
    expect(formatRetryTime(at, NOW)).toBe(
      new Intl.DateTimeFormat(undefined, { timeStyle: "short" }).format(at),
    );
  });

  test("T-365 a retry time 3 days away shows a medium date with a short time", () => {
    const at = NOW + 3 * 86_400_000;
    expect(formatRetryTime(at, NOW)).toBe(
      new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(at),
    );
  });

  test("T-365 the retry time counts from now by default", () => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
    expect(formatRetryTime(NOW + 59_000)).toBe(
      new Intl.DateTimeFormat(undefined, { timeStyle: "medium" }).format(NOW + 59_000),
    );
  });

  test("values are inserted as text, and a name without a value stays", () => {
    expect(fillText(en["error.reference"], { id: "abc" })).toBe("Reference ID: abc");
    expect(fillText("{a} and {b}", { a: 1 })).toBe("1 and {b}");
    expect(fillText("<b>{name}</b>", { name: "<i>x</i>" })).toBe("<b><i>x</i></b>");
    expect(fillText("plain")).toBe("plain");
  });

  test("plural forms follow Intl.PluralRules, with zero only where the text has one", () => {
    const done = en["chat.announce.done"];
    expect(pluralText(done, 0)).toBe("Answer complete. No sources were cited.");
    expect(pluralText(done, 1)).toBe("Answer complete. 1 source.");
    expect(pluralText(done, 3)).toBe("Answer complete. 3 sources.");
    expect(pluralText(en["members.added"], 0)).toBe("0 people added.");
    expect(pluralText(en["memory.usage.facts"], 1, { max: 50 })).toBe("1 of 50 memories saved");
    expect(pluralText(en["memory.usage.facts"], 7, { max: 50 })).toBe("7 of 50 memories saved");
  });

  test("dates, times and sizes follow section 12.7", () => {
    const at = "2026-11-02T14:30:00Z";
    expect(formatDate(at)).toBe(
      new Intl.DateTimeFormat(undefined, { dateStyle: "medium" }).format(new Date(at)),
    );
    expect(formatTime(at)).toBe(
      new Intl.DateTimeFormat(undefined, { timeStyle: "short" }).format(new Date(at)),
    );
    expect(formatDateTime(at)).toBe(
      new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(
        new Date(at),
      ),
    );
    expect(formatSize(52_428_800)).toBe("50.0 MB");
    expect(formatSize(1_572_864)).toBe("1.5 MB");
    expect(formatPercent(9, 10)).toBe(90);
    expect(formatPercent(999, 1000)).toBe(99);
    expect(formatPercent(1, 0)).toBe(0);
  });

  test("a session's last activity: a time today, a weekday this week, a date before", () => {
    const today = NOW - 3_600_000;
    expect(formatLastActivity(today, NOW)).toBe(formatTime(today));
    const yesterday = NOW - 86_400_000;
    expect(formatLastActivity(yesterday, NOW)).toBe(
      new Intl.DateTimeFormat(undefined, {
        weekday: "short",
        hour: "numeric",
        minute: "2-digit",
      }).format(yesterday),
    );
    const sixDaysAgo = new Date(2026, 10, 3 - 6, 9, 0).getTime();
    expect(formatLastActivity(sixDaysAgo, NOW)).toContain(
      new Intl.DateTimeFormat(undefined, { weekday: "short" }).format(sixDaysAgo),
    );
    const longAgo = new Date(2026, 9, 1).getTime();
    expect(formatLastActivity(longAgo, NOW)).toBe(formatDate(longAgo));
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
    expect(formatLastActivity(today)).toBe(formatTime(today));
  });

  test("lengths count Unicode code points, the API's unit", () => {
    expect(codePointLength("abc")).toBe(3);
    expect(codePointLength("😀😀")).toBe(2);
    expect(codePointLength("é中𝄞")).toBe(3);
    expect(codePointLength("\uD800")).toBe(1);
  });
});
