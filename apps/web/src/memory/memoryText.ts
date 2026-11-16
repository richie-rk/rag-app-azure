// The Memory screen's checks and texts (10-web-client.md section 7.3): the fact box keeps at most
// 800 code points (the `MemoryFactRequest.text` limit), Save blocks an empty text and one above
// `limits.maxFactChars` (200) code points on the trimmed text, and each `422` reason has its text.

import { memoryFactRequestText } from "../api/generated/limits.gen";
import type { FactCategory } from "../api/generated/types.gen";
import { en } from "../copy/en";
import { codePointLength, fillText, formatPercent } from "../ui/format";

/** The longest text the fact box keeps, in code points. */
export const FACT_INPUT_MAX = memoryFactRequestText.maxLength;

/** The categories in the API's order (`11-api.md` section 4.8). */
export const FACT_CATEGORIES: readonly FactCategory[] = [
  "role",
  "team",
  "expertise",
  "topic",
  "answer_style",
];

/** The first `max` code points of `text`. The HTML `maxlength` attribute counts UTF-16 code units,
 * so an astral character would count twice there; here it counts once. */
export function keepCodePoints(text: string, max: number = FACT_INPUT_MAX): string {
  return codePointLength(text) <= max ? text : Array.from(text).slice(0, max).join("");
}

/** The reason Save is blocked before sending, or null: `empty` or `too_long`. */
export function factTextReason(text: string, maxFactChars: number): "empty" | "too_long" | null {
  const trimmed = text.trim();
  if (trimmed === "") return "empty";
  return codePointLength(trimmed) > maxFactChars ? "too_long" : null;
}

const REASON_TEXTS: Readonly<Record<string, string>> = {
  empty: en["memory.reason.empty"],
  too_long: en["memory.reason.too_long"],
  contains_link: en["memory.reason.contains_link"],
  contains_code: en["memory.reason.contains_code"],
  contains_instruction: en["memory.reason.contains_instruction"],
  contains_secret: en["memory.reason.contains_secret"],
  duplicate_fact: en["memory.reason.duplicate_fact"],
  memory_full: en["memory.reason.memory_full"],
};

/** `memory.reason.<reason>`, or `error.validation.field` for a reason without a key. */
export function factReasonText(reason: string, maxFactChars: number): string {
  const text = REASON_TEXTS[reason];
  return text === undefined ? en["error.validation.field"] : fillText(text, { max: maxFactChars });
}

/** The usage bar's colour: `warning` from 90%, `error` at 100%. */
export function usageColor(usedTokens: number, maxTokens: number): "brand" | "warning" | "error" {
  const percent = formatPercent(usedTokens, maxTokens);
  if (percent >= 100) return "error";
  return percent >= 90 ? "warning" : "brand";
}
