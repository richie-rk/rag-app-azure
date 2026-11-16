import { describe, expect, test } from "vitest";

import { zTurnView } from "../api/generated/zod.gen";
import { readRepositoryText } from "../test/files";
import { turnErrorTexts, type TurnErrorText } from "./turnErrorTexts";

/** The fixture: sections 2.3 and 2.4 of `11-api.md`, read from the tables themselves. */
function fixture(): Record<string, TurnErrorText> {
  const text = readRepositoryText("docs/spec/11-api.md");
  const section = text.slice(text.indexOf("### 2.3 Stream-only codes"), text.indexOf("### 2.5"));
  const entries: Record<string, TurnErrorText> = {};
  for (const line of section.split("\n").filter((row) => row.startsWith("| `"))) {
    const cells = line.split(" | ").map((cell) => cell.replace(/^\| /, ""));
    const code = (cells[0] ?? "").replaceAll("`", "");
    const retryable = cells[3] ?? "";
    entries[code] = {
      title: cells[1] ?? "",
      text: cells[2] ?? "",
      retryable: retryable === "`true`" || retryable.startsWith("yes"),
    };
  }
  return entries;
}

describe("T-377 the turn error texts", () => {
  test("T-377 hold one entry per value of TurnView.errorCode, equal to the API's tables", () => {
    const codes = zTurnView.shape.errorCode.unwrap().options;
    expect(Object.keys(turnErrorTexts).sort()).toEqual([...codes].sort());
    expect(codes).toHaveLength(15);
    expect(turnErrorTexts).toEqual(fixture());
  });

  test("T-377 the four codes that ask for another question or a new chat offer no Retry", () => {
    const refused = Object.entries(turnErrorTexts)
      .filter(([, entry]) => !entry.retryable)
      .map(([code]) => code)
      .sort();
    expect(refused).toEqual([
      "answer_filtered",
      "model_request_rejected",
      "prompt_too_large",
      "question_filtered",
    ]);
  });
});
