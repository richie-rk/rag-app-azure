// Form validation and field errors (10-web-client.md sections 5.6 and 7.1): Save and Create check
// the draft with the generated request schema and the code-point limits of `limits.gen.ts` before
// sending; a `422` puts each `body.` entry on the field its dotted path names; everything else goes
// to the error bar; focus moves to the first invalid field.

import { useCallback, useRef } from "react";
import type { z } from "zod";

import type { FieldError } from "../../api/generated/types.gen";
import type { ApiError } from "../../api/problem";
import { splitFieldErrors } from "../../api/problem";
import { en } from "../../copy/en";
import { codePointLength, fillText } from "../../ui/format";

/** Field messages, keyed by the field's dotted name (`exampleQuestions.2`). */
export type FieldMessages = ReadonlyMap<string, string>;

/** The client's lines a screen adds to a failure (section 12.3 rule 1): a `428` means the client
 * sent no version or a malformed one, a defect the user can only report. */
export function problemLines(error: ApiError): string[] {
  return error.status === 428 ? [en["error.defect.body"]] : [];
}

/** The `422` entries that match no field, one line each in the error bar (section 7.1). */
export function unmatchedLines(entries: readonly FieldError[]): string[] {
  return entries.map((entry) => `${entry.path}: ${entry.reason}`);
}

export interface RoutedFieldErrors {
  readonly messages: FieldMessages;
  readonly unmatched: readonly FieldError[];
}

/** Routes a `422`'s entries: each `body.<field>` entry shows the screen's text for its reason on
 * that field, or `error.validation.field` when the screen has none. */
export function routeFieldErrors(
  error: ApiError,
  fields: Iterable<string>,
  textFor: (field: string, reason: string) => string | null = () => null,
): RoutedFieldErrors {
  const split = splitFieldErrors(error, fields);
  const messages = new Map<string, string>();
  for (const [field, reason] of split.byField) {
    messages.set(field, textFor(field, reason) ?? en["error.validation.field"]);
  }
  return { messages, unmatched: split.unmatched };
}

/** `project.form.tooLong` for a limit of `max` code points. */
export function tooLongMessage(max: number): string {
  return fillText(en["project.form.tooLong"], { max });
}

/** `project.form.tooLong` when `text` holds more than `max` code points, else null. */
export function tooLongText(text: string, max: number): string | null {
  return codePointLength(text) > max ? tooLongMessage(max) : null;
}

/** The dotted paths a request body fails in its generated schema. Text lengths are left to the
 * code-point checks, because the API counts code points where the schema counts UTF-16 units. */
export function schemaIssuePaths(schema: z.ZodType, body: unknown): string[] {
  const parsed = schema.safeParse(body);
  if (parsed.success) return [];
  return parsed.error.issues
    .filter((issue) => !(issue.code === "too_big" && issue.origin === "string"))
    .map((issue) => issue.path.map(String).join("."));
}

const FOCUSABLE = "input, textarea, button, select";

/** Refs to each field's control, by dotted name, so that a failed submit can focus the first
 * invalid field in the form's order. */
export function useFieldFocus() {
  const elements = useRef(new Map<string, HTMLElement>());
  const register = useCallback(
    (name: string) => (element: HTMLElement | null) => {
      if (element === null) elements.current.delete(name);
      else elements.current.set(name, element);
    },
    [],
  );
  const focusFirst = useCallback((order: readonly string[], invalid: FieldMessages) => {
    const first = order.find((name) => invalid.has(name));
    const element = first === undefined ? undefined : elements.current.get(first);
    if (element === undefined) return;
    // A composite field registers its wrapper; its first control takes the focus.
    const target = element.matches(FOCUSABLE)
      ? element
      : (element.querySelector<HTMLElement>(FOCUSABLE) ?? element);
    target.focus();
  }, []);
  return { register, focusFirst };
}
