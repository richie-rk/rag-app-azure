import { describe, expect, test } from "vitest";

import { PLAIN_TEXT_ANSWER_ABOVE } from "../../platform/constants";
import { AnswerNotCopyable, answerToPlainText } from "./copy";
import type { RenderContext } from "./policy";

const citations = [{ number: 1, title: "Expenses guide" }];
const twelve = Array.from(
  { length: 12 },
  (_, index) => `https://docs.example/n${String(index + 1)}`,
);
const context: RenderContext = {
  citations,
  allowedLinks: ["https://docs.example/a", "https://Docs.Example/b", ...twelve],
};

function copy(markdown: string, value: RenderContext = context): string {
  return answerToPlainText(markdown, value);
}

describe("T-386 Copy answer's text", () => {
  test("T-386 a citation stays [n], before and after citations arrive", () => {
    expect(copy("Claims ⟦1⟧.")).toBe("Claims [1].\n");
    expect(copy("Claims ⟦1⟧.", {})).toBe("Claims [1].\n");
  });

  test("T-386 an allowed link copies as its text and normalised URL", () => {
    expect(copy("Read [the guide](https://docs.example/b).")).toBe(
      "Read the guide (https://docs.example/b) .\n",
    );
  });

  test("T-386 a space follows the URL when a citation or a word follows directly", () => {
    expect(copy("[guide](https://docs.example/a)⟦1⟧")).toBe("guide (https://docs.example/a) [1]\n");
    expect(copy("[guide](https://docs.example/a) next")).toBe(
      "guide (https://docs.example/a) next\n",
    );
  });

  test("T-386 the 11th and 12th allowed links copy as their text and host", () => {
    const markdown = twelve.map((url, index) => `[L${String(index + 1)}](${url})`).join(" ");
    const text = copy(markdown);
    expect(text).toContain("L10 (https://docs.example/n10) L11 (docs.example) L12 (docs.example)");
  });

  test("T-386 a link outside allowedLinks copies as its text alone", () => {
    const text = copy("[click here](https://evil.example/c?d=secret) now");
    expect(text).toBe("click here now\n");
  });

  test("T-386 refused bare and www. URLs copy as the marker, with their full stop", () => {
    expect(copy("Go https://evil.example/c?d=secret.")).toBe("Go [link removed].\n");
    expect(copy("Go www.evil.example/c?d=secret")).toBe("Go [link removed]\n");
  });

  test("T-386 images add no URL; an alt text that is a refused URL copies as the marker", () => {
    expect(copy("![x](https://img.example/a.png)")).toBe("x\n");
    expect(copy("![https://evil.example/p?d=1](https://img.example/a.png)")).toBe(
      "[link removed]\n",
    );
  });

  test("T-386 raw HTML copies as the literal text the policy made of it", () => {
    expect(copy("<b>x</b> and <img src=https://evil.example/p?d=secret>")).toBe(
      "<b>x</b> and <img src=[link removed]>\n",
    );
  });

  test("T-386 code copies unchanged except refused URLs; allowed URLs stay as written", () => {
    const markdown = [
      "```",
      "const a = 1; // https://evil.example/p?d=secret",
      "fetch('https://docs.example/a')",
      "```",
      "",
      "Inline `![x](https://evil.example/p?d=secret)` and https://docs.example/a in prose.",
    ].join("\n");
    expect(copy(markdown)).toBe(
      "const a = 1; // [link removed]\nfetch('https://docs.example/a')\n" +
        "Inline ![x]([link removed]) and https://docs.example/a (https://docs.example/a) in prose.\n",
    );
  });

  test("T-386 paragraphs, headings, list items and table rows end with line breaks", () => {
    const markdown = [
      "# Title",
      "",
      "First.",
      "",
      "- one",
      "- two",
      "  - nested",
      "",
      "3. three",
      "4. four",
      "",
      "Between.",
      "",
      "1. loose",
      "",
      "2. list",
      "",
      "| a | b |",
      "|---|---|",
      "| 1 | 2 |",
      "",
      "> quoted",
      "",
      "line  ",
      "break",
      "",
      "---",
      "",
      "End.",
    ].join("\n");
    expect(copy(markdown)).toBe(
      "Title\nFirst.\n- one\n- two\n- nested\n3. three\n4. four\nBetween.\n1. loose\n2. list\n" +
        "a\tb\n1\t2\nquoted\nline\nbreak\nEnd.\n",
    );
  });

  test("T-386 an answer longer than 200,000 characters can't be copied", () => {
    expect(() => copy("a".repeat(PLAIN_TEXT_ANSWER_ABOVE + 1))).toThrow(AnswerNotCopyable);
  });

  test("T-386 a tree that can't be built can't be copied", () => {
    const broken = {
      allowedLinks: null,
      get citations(): never {
        throw new Error("broken");
      },
    };
    expect(() => copy("Claims ⟦1⟧", broken)).toThrow(AnswerNotCopyable);
  });
});
