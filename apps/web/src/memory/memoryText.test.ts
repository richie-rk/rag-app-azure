import { describe, expect, test } from "vitest";

import {
  FACT_CATEGORIES,
  FACT_INPUT_MAX,
  factReasonText,
  factTextReason,
  keepCodePoints,
  usageColor,
} from "./memoryText";

describe("the memory texts and checks", () => {
  test("T-396 the fact box keeps the first 800 code points, an astral character counting one", () => {
    expect(FACT_INPUT_MAX).toBe(800);
    expect(keepCodePoints("a".repeat(900))).toBe("a".repeat(800));
    const astral = "😀".repeat(801);
    expect(Array.from(keepCodePoints(astral))).toHaveLength(800);
    expect(keepCodePoints("short")).toBe("short");
  });

  test("T-396 Save is blocked for an empty trimmed text and one above maxFactChars", () => {
    expect(factTextReason("   ", 200)).toBe("empty");
    expect(factTextReason(` ${"x".repeat(200)} `, 200)).toBeNull();
    expect(factTextReason("x".repeat(201), 200)).toBe("too_long");
    expect(factTextReason("😀".repeat(200), 200)).toBeNull();
  });

  test("T-394 each reason has its text; one without a key shows error.validation.field", () => {
    expect(factReasonText("too_long", 200)).toBe(
      "A memory can be up to 200 characters. Shorten it, then save.",
    );
    expect(factReasonText("contains_link", 200)).toBe(
      "Memories can't include web addresses. Remove the link, then save.",
    );
    expect(factReasonText("memory_full", 200)).toBe(
      "Your memory is full. Delete or shorten other memories, then save this one.",
    );
    expect(factReasonText("invalid_characters", 200)).toBe(
      "This value isn't valid. Check it and correct it.",
    );
  });

  test("T-396 the usage bar turns warning at 90% and error at 100%", () => {
    expect(usageColor(899, 1000)).toBe("brand");
    expect(usageColor(900, 1000)).toBe("warning");
    expect(usageColor(999, 1000)).toBe("warning");
    expect(usageColor(1000, 1000)).toBe("error");
    expect(usageColor(1200, 1000)).toBe("error");
  });

  test("the categories follow the API's order", () => {
    expect(FACT_CATEGORIES).toEqual(["role", "team", "expertise", "topic", "answer_style"]);
  });
});
