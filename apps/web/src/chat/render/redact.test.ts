import { describe, expect, test } from "vitest";

import {
  MAX_ALLOWED_URL_LENGTH,
  allowedLinkHref,
  allowedLinkSet,
  normaliseUrl,
  redactRefusedUrls,
  refusedUrlRanges,
  trimCandidate,
} from "./redact";

const ALLOWED = ["https://docs.example/a", "https://docs.example/b?x=1"];
const REMOVED = "[link removed]";

describe("the normalisation rule", () => {
  test("is new URL(s).href, or null when it doesn't parse", () => {
    expect(normaliseUrl("HTTPS://Docs.Example:443/a/../b")).toBe("https://docs.example/b");
    expect(normaliseUrl("https://bücher.example/c")).toBe("https://xn--bcher-kva.example/c");
    expect(normaliseUrl("not a url")).toBeNull();
  });

  test("allowedLinkSet normalises each entry, drops one that doesn't parse, and keeps null", () => {
    expect(allowedLinkSet(null)).toBeNull();
    expect(allowedLinkSet(undefined)).toBeNull();
    expect([...(allowedLinkSet(["https://Docs.Example/a", "::bad"]) ?? [])]).toEqual([
      "https://docs.example/a",
    ]);
  });

  test("T-381 an allowed link is https, without credentials, at most 2,048 characters and listed", () => {
    const allowed = allowedLinkSet(["https://docs.example/a", "http://docs.example/h"]);
    expect(allowedLinkHref("https://DOCS.example/a", allowed)).toBe("https://docs.example/a");
    expect(allowedLinkHref("https://docs.example/a", null)).toBeNull();
    expect(allowedLinkHref("https://user@docs.example/a", allowed)).toBeNull();
    expect(allowedLinkHref("https://:pw@docs.example/a", allowed)).toBeNull();
    expect(allowedLinkHref("http://docs.example/h", allowed)).toBeNull();
    expect(allowedLinkHref("https://docs.example/other", allowed)).toBeNull();
    expect(allowedLinkHref("::bad", allowed)).toBeNull();
    const long = `https://docs.example/${"a".repeat(MAX_ALLOWED_URL_LENGTH)}`;
    expect(allowedLinkHref(long, allowedLinkSet([long]))).toBeNull();
  });
});

describe("trailing punctuation", () => {
  test.each([
    ["https://e.example/p.", "https://e.example/p"],
    ["https://e.example/p?!,:*_~", "https://e.example/p"],
    ["https://e.example/p)", "https://e.example/p"],
    ["https://e.example/wiki/Term_(topic)", "https://e.example/wiki/Term_(topic)"],
    ["https://e.example/p&amp;", "https://e.example/p"],
    ["https://e.example/p&amp;.", "https://e.example/p"],
    ["https://e.example/p;", "https://e.example/p;"],
    ["https://e.example/p&;", "https://e.example/p&;"],
  ])("%s trims to %s", (candidate, trimmed) => {
    expect(trimCandidate(candidate)).toBe(trimmed);
  });
});

describe("T-419 refused URLs in text", () => {
  test("T-419 a refused bare URL becomes the marker, and trailing punctuation stays after it", () => {
    expect(redactRefusedUrls("See https://evil.example/c?d=secret now", ALLOWED)).toBe(
      `See ${REMOVED} now`,
    );
    expect(redactRefusedUrls("https://evil.example/p.", ALLOWED)).toBe(`${REMOVED}.`);
    expect(redactRefusedUrls("(https://evil.example/p)", ALLOWED)).toBe(`(${REMOVED})`);
    expect(redactRefusedUrls("https://evil.example/p&amp;", ALLOWED)).toBe(`${REMOVED}&amp;`);
  });

  test("T-419 www. addresses in any letter case, other schemes and protocol-relative addresses go", () => {
    expect(redactRefusedUrls("www.evil.example/p", ALLOWED)).toBe(REMOVED);
    expect(redactRefusedUrls("WWW.evil.example/p", ALLOWED)).toBe(REMOVED);
    expect(redactRefusedUrls("ftp://evil.example/p", ALLOWED)).toBe(REMOVED);
    expect(redactRefusedUrls("<img src=//evil.example/p>", ALLOWED)).toBe(`<img src=${REMOVED}>`);
    expect(redactRefusedUrls("http://docs.example/a", ALLOWED)).toBe(REMOVED);
  });

  test("T-419 an allowed URL stays as written, with its punctuation", () => {
    expect(redactRefusedUrls("https://docs.example/a, then", ALLOWED)).toBe(
      "https://docs.example/a, then",
    );
    expect(redactRefusedUrls("HTTPS://DOCS.example/b?x=1", ALLOWED)).toBe(
      "HTTPS://DOCS.example/b?x=1",
    );
  });

  test("T-419 before allowedLinks arrives every candidate is redacted", () => {
    expect(redactRefusedUrls("https://docs.example/a", null)).toBe(REMOVED);
    expect(redactRefusedUrls("https://docs.example/a", undefined)).toBe(REMOVED);
    expect(redactRefusedUrls("https://docs.example/a", ALLOWED)).toBe("https://docs.example/a");
  });

  test("T-419 a zero-width space or a soft hyphen can't split a URL, and the range covers it", () => {
    const split = "go https:​//evil.example/p now";
    expect(redactRefusedUrls(split, ALLOWED)).toBe(`go ${REMOVED} now`);
    expect(refusedUrlRanges(split, ALLOWED)).toEqual([{ start: 3, end: 26 }]);
    expect(redactRefusedUrls("https://evil.­example/p", ALLOWED)).toBe(REMOVED);
    // Format characters outside a candidate stay where they are.
    expect(redactRefusedUrls("​plain​", ALLOWED)).toBe("​plain​");
    expect(redactRefusedUrls("\u{E0001}https://evil.example/\u{E0001}p", ALLOWED)).toBe(
      `\u{E0001}${REMOVED}`,
    );
  });

  test("T-419 of 40 letters directly before ://, the first 8 stay", () => {
    const letters = "a".repeat(40);
    expect(redactRefusedUrls(`${letters}://evil.example/p`, ALLOWED)).toBe(
      `${"a".repeat(8)}${REMOVED}`,
    );
  });

  test("T-419 emails, mailto, bare domains and UNC paths are no candidates", () => {
    for (const value of [
      "name@example.net",
      "mailto:name@example.net",
      "example.net/abc",
      "\\\\host\\share",
      "a//b",
      "www",
      "https://",
      "https://.",
    ]) {
      expect(redactRefusedUrls(value, ALLOWED)).toBe(value);
    }
  });

  test("T-419 a URL that doesn't parse is refused, and the function never throws", () => {
    expect(redactRefusedUrls("https://[::bad/p", ALLOWED)).toBe(REMOVED);
  });

  test("T-419 returns within 200 ms for 200,000 letters with no white space", () => {
    const text = "a".repeat(200_000);
    const started = performance.now();
    expect(redactRefusedUrls(text, ALLOWED)).toBe(text);
    expect(performance.now() - started).toBeLessThan(200);
  });

  test("T-419 a long run of closing parentheses trims in linear time", () => {
    const text = `https://evil.example/p${")".repeat(100_000)}`;
    const started = performance.now();
    expect(redactRefusedUrls(text, ALLOWED)).toBe(`${REMOVED}${")".repeat(100_000)}`);
    expect(performance.now() - started).toBeLessThan(200);
  });
});
