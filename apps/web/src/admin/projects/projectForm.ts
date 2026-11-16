// The project form's values, checks and request bodies (10-web-client.md section 7.6): Name,
// Description, Example questions, Instructions for answers and Add new users automatically, with
// the limits of `limits.gen.ts`, counted in code points.

import "../../bootstrap/zodSetup";
import {
  projectCreateDescription,
  projectCreateExampleQuestions,
  projectCreateInstructions,
  projectCreateName,
} from "../../api/generated/limits.gen";
import type { ProjectCreate, ProjectUpdate } from "../../api/generated/types.gen";
import { zProjectCreate, zProjectUpdate } from "../../api/generated/zod.gen";
import type { ApiError } from "../../api/problem";
import { en } from "../../copy/en";
import { changesFrom } from "../shared/conflict";
import {
  routeFieldErrors,
  schemaIssuePaths,
  tooLongMessage,
  tooLongText,
  type FieldMessages,
  type RoutedFieldErrors,
} from "../shared/formErrors";
import type { AdminProject } from "../shared/queries";

export interface ProjectFormValues {
  readonly name: string;
  readonly description: string;
  /** One entry per row on screen, blank rows included. */
  readonly exampleQuestions: readonly string[];
  readonly instructions: string;
  readonly isDefault: boolean;
}

export const EMPTY_PROJECT_FORM: ProjectFormValues = {
  name: "",
  description: "",
  exampleQuestions: [],
  instructions: "",
  isDefault: false,
};

export const PROJECT_LIMITS = {
  name: projectCreateName.maxLength,
  description: projectCreateDescription.maxLength,
  exampleQuestions: projectCreateExampleQuestions.maxItems,
  exampleQuestion: projectCreateExampleQuestions.itemsMaxLength,
  instructions: projectCreateInstructions.maxLength,
} as const;

/** The form's values for a loaded project. */
export function projectFormValues(project: AdminProject): ProjectFormValues {
  return {
    name: project.name,
    description: project.description,
    exampleQuestions: project.exampleQuestions,
    instructions: project.instructions,
    isDefault: project.isDefault,
  };
}

/** The rows that hold a question, in order: a row left blank isn't sent. Index `i` of the sent
 * list is row `rows[i]`, so a `422` at `body.exampleQuestions.<i>` finds its row. */
export function sentExampleRows(questions: readonly string[]): number[] {
  return questions.flatMap((question, row) => (question.trim() === "" ? [] : [row]));
}

/** The values as the API would compare them: blank example rows dropped. */
export function normalizedProject(values: ProjectFormValues): ProjectFormValues {
  const rows = sentExampleRows(values.exampleQuestions);
  return {
    ...values,
    exampleQuestions: rows.map((row) => values.exampleQuestions[row] ?? ""),
  };
}

/** The dotted field names in the form's order, for focus on a failed submit. */
export function projectFieldOrder(values: ProjectFormValues): string[] {
  return [
    "name",
    "description",
    ...values.exampleQuestions.map((_, row) => `exampleQuestions.${String(row)}`),
    "instructions",
    "isDefault",
  ];
}

/** The code-point limit of a project field, by dotted name. */
function limitOf(field: string): number | null {
  if (field === "name") return PROJECT_LIMITS.name;
  if (field === "description") return PROJECT_LIMITS.description;
  if (field === "instructions") return PROJECT_LIMITS.instructions;
  if (field.startsWith("exampleQuestions.")) return PROJECT_LIMITS.exampleQuestion;
  return null;
}

/** The screen's text for a `422` reason on a project field, or null for `error.validation.field`. */
export function projectReasonText(field: string, reason: string): string | null {
  if (field === "name" && (reason === "required" || reason === "too_short")) {
    return en["project.form.required"];
  }
  const limit = limitOf(field);
  return reason === "too_long" && limit !== null ? tooLongMessage(limit) : null;
}

/** The checks before Save or Create (section 7.1): Name is required, and every text keeps to its
 * limit. Messages are keyed by dotted field name. */
export function checkProjectForm(values: ProjectFormValues): Map<string, string> {
  const messages = new Map<string, string>();
  const name = values.name.trim();
  if (name === "") messages.set("name", en["project.form.required"]);
  const checks: [string, string, number][] = [
    ["name", name, PROJECT_LIMITS.name],
    ["description", values.description.trim(), PROJECT_LIMITS.description],
    ...values.exampleQuestions.map((question, row): [string, string, number] => [
      `exampleQuestions.${String(row)}`,
      question.trim(),
      PROJECT_LIMITS.exampleQuestion,
    ]),
    ["instructions", values.instructions.trim(), PROJECT_LIMITS.instructions],
  ];
  for (const [field, text, max] of checks) {
    const tooLong = tooLongText(text, max);
    if (tooLong !== null && !messages.has(field)) messages.set(field, tooLong);
  }
  return messages;
}

/** Maps a schema path of the sent body back to the form's field (example rows renumbered). */
function formField(path: string, values: ProjectFormValues): string {
  const match = /^exampleQuestions\.(\d+)$/.exec(path);
  if (match?.[1] === undefined) return path;
  const row = sentExampleRows(values.exampleQuestions)[Number(match[1])];
  return row === undefined ? path : `exampleQuestions.${String(row)}`;
}

/** `adminCreateProject`'s body: every field, with the chosen people in the order chosen. */
export function createProjectBody(
  values: ProjectFormValues,
  initialMemberIds: readonly string[],
): ProjectCreate {
  const normalized = normalizedProject(values);
  return {
    name: normalized.name,
    description: normalized.description,
    instructions: normalized.instructions,
    exampleQuestions: [...normalized.exampleQuestions],
    isDefault: normalized.isDefault,
    initialMemberIds: [...initialMemberIds],
  };
}

/** `adminUpdateProject`'s body: only the fields that differ from `base` (section 7.4 step 2). */
export function updateProjectBody(
  base: ProjectFormValues,
  draft: ProjectFormValues,
): ProjectUpdate {
  const changes = changesFrom(normalizedProject(base), normalizedProject(draft));
  const body: ProjectUpdate = {};
  if (changes.name !== undefined) body.name = changes.name;
  if (changes.description !== undefined) body.description = changes.description;
  if (changes.instructions !== undefined) body.instructions = changes.instructions;
  if (changes.exampleQuestions !== undefined) body.exampleQuestions = [...changes.exampleQuestions];
  if (changes.isDefault !== undefined) body.isDefault = changes.isDefault;
  return body;
}

/** The fields a body fails in its generated schema, as form fields. */
export function projectSchemaFields(
  body: ProjectCreate | ProjectUpdate,
  values: ProjectFormValues,
  create: boolean,
): FieldMessages {
  const paths = schemaIssuePaths(create ? zProjectCreate : zProjectUpdate, body);
  return new Map(paths.map((path) => [formField(path, values), en["error.validation.field"]]));
}

/** A `422` routed to the form (section 7.1): example question indices of the sent list renumbered
 * to the rows on screen, each reason in the screen's text. `extraFields` names fields outside the
 * project form, such as the Members picker's `initialMemberIds.<i>`. */
export function routeProjectErrors(
  error: ApiError,
  values: ProjectFormValues,
  extraFields: readonly string[] = [],
): RoutedFieldErrors {
  const renumbered: ApiError = {
    ...error,
    errors: error.errors.map((entry) =>
      entry.path.startsWith("body.")
        ? { ...entry, path: `body.${formField(entry.path.slice("body.".length), values)}` }
        : entry,
    ),
  };
  return routeFieldErrors(
    renumbered,
    [...projectFieldOrder(values), ...extraFields],
    projectReasonText,
  );
}
