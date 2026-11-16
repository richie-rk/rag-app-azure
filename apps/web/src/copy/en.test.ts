import { describe, expect, test } from "vitest";

import { readRepositoryText } from "../test/files";
import { displayNames, en, type PluralText } from "./en";

const SPEC = readRepositoryText("docs/spec/10-web-client.md");
const KEY_PREFIXES =
  /^(app|access|session|shell|admin|confirm|memory|projects?|members|projectIndexes|indexes|index|documents|documentPanel|runs?|chat|citation|feedback|upload|error)\./;

interface Expected {
  readonly key: string;
  readonly text: string;
}

/** A trailing note in parentheses that holds code (`{max}` is …) or starts with "with" or
 * "options" describes the text rather than being part of it. */
function withoutNote(text: string): string {
  return text.replace(/ \((?:with |options: |[^)]*`)[^)]*\)\.?$/, "");
}

/** Every row of every `| Key | Text |` table, keys spelled out (`.body` after
 * `memory.deleteConfirm.title` is `memory.deleteConfirm.body`). */
function textTableRows(): { keys: string[]; text: string }[] {
  const rows: { keys: string[]; text: string }[] = [];
  let inTable = false;
  for (const line of SPEC.split("\n")) {
    if (line.startsWith("| Key | Text |")) {
      inTable = true;
      continue;
    }
    if (!line.startsWith("|")) {
      inTable = false;
      continue;
    }
    if (!inTable || line.startsWith("|---")) continue;
    const [first = "", text = ""] = line
      .slice(1, -1)
      .split(" | ")
      .map((cell) => cell.trim());
    let base = "";
    const keys = [...first.matchAll(/`(\.?[A-Za-z_.*]+)`/g)].map((match) => {
      const name = match[1] ?? "";
      if (!name.startsWith(".")) {
        base = name;
        return name;
      }
      return base.slice(0, base.lastIndexOf(".")) + name;
    });
    rows.push({ keys, text });
  }
  return rows;
}

function section(start: string, end: string): string {
  const from = SPEC.indexOf(start);
  return SPEC.slice(from, SPEC.indexOf(end, from));
}

/** Keys the access, shell and error-screen tables of sections 4.9, 6.3 and 6.5 define inline. */
function inlineKeys(): Set<string> {
  const tables = [
    section("### 4.9 The access screens", "## 5."),
    section("### 6.3 The app shell", "### 6.4"),
    section("### 6.5 A release under an open tab", "## 7."),
  ];
  const keys = new Set<string>();
  for (const table of tables) {
    for (const line of table.split("\n").filter((each) => each.startsWith("|"))) {
      for (const match of line.matchAll(/`([a-z][A-Za-z]*(?:\.[A-Za-z_]+)+)`/g)) {
        const key = match[1] ?? "";
        if (KEY_PREFIXES.test(key) && !key.includes("*")) keys.add(key);
      }
    }
  }
  return keys;
}

/** Section 4.9's `key`: text pairs. */
function accessTexts(): Expected[] {
  const table = section("### 4.9 The access screens", "## 5.");
  return [...table.matchAll(/`([a-z][A-Za-z.]+)`: ([^·|]+?)(?= · `| \|)/g)].map((match) => ({
    key: match[1] ?? "",
    text: withoutNote((match[2] ?? "").trim()),
  }));
}

function parsePlural(text: string): PluralText {
  const forms = Object.fromEntries(
    text.split(" / ").map((part) => {
      const at = part.indexOf(": ");
      return [part.slice(0, at), part.slice(at + 2)];
    }),
  );
  return forms as unknown as PluralText;
}

const entries = en as unknown as Record<string, unknown>;

describe("the copy", () => {
  test("T-416 en.ts holds every key of the text tables with its text, and nothing else", () => {
    const expectedKeys = new Set<string>(inlineKeys());
    for (const { keys, text } of textTableRows()) {
      const [onlyKey] = keys;
      if (keys.length === 1 && onlyKey?.endsWith(".*")) {
        const prefix = onlyKey.slice(0, -1);
        const family = Object.keys(entries).filter((key) => key.startsWith(prefix));
        expect(
          family.map((key) => entries[key]),
          onlyKey,
        ).toEqual(text.split(" / "));
        family.forEach((key) => expectedKeys.add(key));
        continue;
      }
      if (keys.length === 1 && /^(zero|one): /.test(text)) {
        expect(entries[onlyKey ?? ""], onlyKey).toEqual(parsePlural(text));
        expectedKeys.add(onlyKey ?? "");
        continue;
      }
      const texts = keys.length === 1 ? [text] : text.split(" / ");
      expect(texts, keys.join(", ")).toHaveLength(keys.length);
      keys.forEach((key, index) => {
        expectedKeys.add(key);
        const value = entries[key];
        if (typeof value === "string") expect(value, key).toBe(withoutNote(texts[index] ?? ""));
        else expect(value, key).toBeDefined();
      });
    }
    expect(Object.keys(entries).sort()).toEqual([...expectedKeys].sort());
  });

  test("T-416 the access screens' texts are section 4.9's", () => {
    const pairs = accessTexts();
    expect(pairs.length).toBeGreaterThanOrEqual(20);
    for (const { key, text } of pairs) expect(entries[key], key).toBe(text);
  });

  test("T-416 the texts with notes and options keep only the text", () => {
    expect(en["chat.heading"]).toEqual({ new: "Chat", session: "Chat: {title}" });
    expect(en["projects.statusFilter"]).toEqual({
      label: "Status",
      options: { active: "Active", archived: "Archived", all: "All" },
    });
    expect(en["documents.stateFilter"]).toEqual({
      label: "State",
      options: { all: "All", ingested: "Ingested", in_progress: "In progress", failed: "Failed" },
    });
    expect(en["memory.reason.too_long"]).toBe(
      "A memory can be up to {max} characters. Shorten it, then save.",
    );
    expect(en["app.starting"]).toBe("Starting DocChat…");
  });

  test("T-416 the two product strings appear word for word", () => {
    expect(en["chat.turn.broadened"]).toBe("Searching more broadly for better matches");
    expect(en["upload.result.update"]).toBe("This document is already ingested. Update it?");
  });

  test("display names cover section 7.12's values", () => {
    expect(displayNames.indexStatus.reingest_required).toBe("Re-ingest needed");
    expect(displayNames.itemState.succeeded).toBe("Done");
    expect(displayNames.factCategory.answer_style).toBe("Answer style");
    expect(displayNames.feedbackCategory.not_relevant).toBe("Not relevant");
    expect(Object.values(displayNames.mediaType)).toEqual([
      "PDF document",
      "Word document",
      "PowerPoint presentation",
    ]);
  });
});
