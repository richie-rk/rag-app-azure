// Announcing streamed text (10-web-client.md section 8.18): each live turn started in this tab is
// read out sentence by sentence through the `answerLog` region, as plain words with citation
// tokens removed and every refused URL redacted, so a screen reader never hears raw Markdown.

import type { Nodes as MdastNodes } from "mdast";
import remarkParse from "remark-parse";
import { unified } from "unified";

import { appendToAnswerLog } from "../../a11y/announce";
import type { LiveTurn, TurnSpeech } from "../stream/turnStore";
import { redactRefusedUrls } from "./redact";

/** `.`, `!`, `?` or `:` followed by white space, or a line break. */
const BOUNDARY = /[.!?:](?=\s)|\n/g;

/** A citation token with the one space before it, as `strip_citation_tokens` removes it
 * (`12-chat-pipeline.md` section 11.6), so "days ⟦1⟧." is read as "days.". */
const SPOKEN_TOKEN = / ?⟦[1-9][0-9]?⟧/g;

/** Nodes whose children read as one line of text; between the children of any other node a
 * space goes in, so two paragraphs don't run together. */
const INLINE_PARENTS: ReadonlySet<string> = new Set([
  "paragraph",
  "heading",
  "emphasis",
  "strong",
  "delete",
  "link",
  "linkReference",
  "tableCell",
]);

function textValues(node: MdastNodes): string {
  if ("value" in node) return node.value;
  // A line break, a rule or an image: a pause between words.
  if (!("children" in node)) return " ";
  return node.children
    .map((child) => textValues(child))
    .join(INLINE_PARENTS.has(node.type) ? "" : " ");
}

/** What is spoken for `chunk`: citation tokens removed, the Markdown parsed and reduced to its
 * text values, white space collapsed, then refused URLs redacted with the turn's allowed links as
 * known now (absent while the answer streams, so every URL is `[link removed]`). */
export function speakableText(chunk: string, allowedLinks: readonly string[] | null): string {
  const tree = unified().use(remarkParse).parse(chunk.replace(SPOKEN_TOKEN, ""));
  const words = textValues(tree).replace(/\s+/g, " ").trim();
  return redactRefusedUrls(words, allowedLinks);
}

/** The offset just past the last boundary in `pending`, or 0 when it has none yet. */
export function spokenLength(pending: string): number {
  let end = 0;
  for (const match of pending.matchAll(BOUNDARY)) end = match.index + 1;
  return end;
}

/** The `TurnSpeech` the turn store calls: `feed` after new text, `flush` before an outcome's
 * announcement. `spokenUpTo` is kept per turn. */
export function createTurnSpeech(append: (text: string) => void = appendToAnswerLog): TurnSpeech {
  const spokenUpTo = new Map<string, number>();
  const speak = (text: string, turn: LiveTurn) => {
    const words = speakableText(text, turn.allowedLinks);
    if (words !== "") append(words);
  };
  return {
    feed(turn) {
      const from = spokenUpTo.get(turn.clientTurnId) ?? 0;
      const pending = turn.text.slice(from);
      const end = spokenLength(pending);
      if (end === 0) return;
      spokenUpTo.set(turn.clientTurnId, from + end);
      speak(pending.slice(0, end), turn);
    },
    flush(turn) {
      const from = spokenUpTo.get(turn.clientTurnId) ?? 0;
      spokenUpTo.set(turn.clientTurnId, turn.text.length);
      speak(turn.text.slice(from), turn);
    },
  };
}
