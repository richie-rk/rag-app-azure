// Every formatter of the client (10-web-client.md sections 5.10, 12.2 and 12.7): inserted values,
// plural forms, dates and times in the browser's locale, sizes and percentages.

import type { PluralText } from "../copy/en";
import { RETRY_TIME_WITH_SECONDS_BELOW_S } from "../platform/constants";

export type TextValues = Readonly<Record<string, string | number>>;

/** Inserts `{name}` values into a text from `src/copy/en.ts`; a name without a value stays. The
 * result is always rendered as a text node. */
export function fillText(template: string, values: TextValues = {}): string {
  return template.replace(/\{(\w+)\}/g, (match, name: string) => {
    const value = values[name];
    return value === undefined ? match : String(value);
  });
}

const pluralRules = new Intl.PluralRules("en");

/** Picks the plural form for `count` (the `zero` form only where the text has one) and inserts
 * `{count}` and the other values. */
export function pluralText(entry: PluralText, count: number, values: TextValues = {}): string {
  const template =
    count === 0 && entry.zero !== undefined
      ? entry.zero
      : pluralRules.select(count) === "one"
        ? entry.one
        : entry.other;
  return fillText(template, { ...values, count });
}

function sameLocalDay(a: Date, b: Date): boolean {
  return (
    a.getFullYear() === b.getFullYear() &&
    a.getMonth() === b.getMonth() &&
    a.getDate() === b.getDate()
  );
}

/** The time a refused request may be repeated, for `error.retryAt` (section 5.10): with seconds
 * when less than 60 s away, a short time later today, otherwise a medium date with a short time. */
export function formatRetryTime(at: number, now: number = Date.now()): string {
  const when = new Date(at);
  if (at - now < RETRY_TIME_WITH_SECONDS_BELOW_S * 1000) {
    return new Intl.DateTimeFormat(undefined, { timeStyle: "medium" }).format(when);
  }
  if (sameLocalDay(when, new Date(now))) {
    return new Intl.DateTimeFormat(undefined, { timeStyle: "short" }).format(when);
  }
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(
    when,
  );
}

/** A date as a medium date, such as `updatedAt` or `grantedAt`. */
export function formatDate(value: string | number | Date): string {
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium" }).format(new Date(value));
}

/** A time as a short time. */
export function formatTime(value: string | number | Date): string {
  return new Intl.DateTimeFormat(undefined, { timeStyle: "short" }).format(new Date(value));
}

/** A medium date with a short time. */
export function formatDateTime(value: string | number | Date): string {
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(
    new Date(value),
  );
}

const DAY_MS = 86_400_000;

/** A session's last activity (section 8.2): a short time today, a short weekday and time in the
 * previous 6 days, otherwise a medium date. */
export function formatLastActivity(
  value: string | number | Date,
  now: number = Date.now(),
): string {
  const when = new Date(value);
  const today = new Date(now);
  if (sameLocalDay(when, today)) return formatTime(when);
  const startOfToday = new Date(today.getFullYear(), today.getMonth(), today.getDate()).getTime();
  if (when.getTime() >= startOfToday - 6 * DAY_MS && when.getTime() < startOfToday) {
    // timeStyle can't be combined with weekday, so the short time is spelled out.
    return new Intl.DateTimeFormat(undefined, {
      weekday: "short",
      hour: "numeric",
      minute: "2-digit",
    }).format(when);
  }
  return formatDate(when);
}

/** A file size: bytes ÷ 1,048,576 with one decimal place and the unit MB. */
export function formatSize(bytes: number): string {
  return `${(bytes / 1_048_576).toFixed(1)} MB`;
}

/** A share as a whole percentage, rounded down. */
export function formatPercent(part: number, whole: number): number {
  if (whole <= 0) return 0;
  return Math.floor((part / whole) * 100);
}

/** The number of Unicode code points, the API's unit for text limits. */
export function codePointLength(text: string): number {
  // A surrogate pair is two UTF-16 code units but one code point.
  return text.length - (text.match(/[\uD800-\uDBFF][\uDC00-\uDFFF]/g)?.length ?? 0);
}
