// The index form's values, checks and request bodies (10-web-client.md sections 7.7 and 7.8),
// shared by New index and Edit settings: Name, Description, Ingestion method with its settings,
// Embedding model, Vector dimensions and Language.

import "../../bootstrap/zodSetup";
import { indexCreateDescription, indexCreateDisplayName } from "../../api/generated/limits.gen";
import type { IndexCreate, IndexUpdate } from "../../api/generated/types.gen";
import { zIndexCreate, zIndexUpdate } from "../../api/generated/zod.gen";
import type { ApiError } from "../../api/problem";
import { en } from "../../copy/en";
import { changesFrom, valuesEqual } from "../shared/conflict";
import {
  routeFieldErrors,
  schemaIssuePaths,
  tooLongMessage,
  tooLongText,
  type FieldMessages,
  type RoutedFieldErrors,
} from "../shared/formErrors";
import {
  initialParameters,
  invalidParameters,
  parameterField,
  parametersToSend,
  parseParameterSchema,
  type ParamSchema,
  type ParamValues,
} from "../shared/ParamForm";
import type { IndexDetail, IndexOptions, MethodOption } from "../shared/queries";

export interface IndexFormValues {
  readonly displayName: string;
  readonly description: string;
  /** The method's key and version, as `methodValue` writes them. */
  readonly method: string;
  readonly methodParameters: ParamValues;
  readonly embeddingModel: string;
  readonly embeddingDimensions: number;
  readonly languageAnalyzer: string;
}

export const INDEX_LIMITS = {
  displayName: indexCreateDisplayName.maxLength,
  description: indexCreateDescription.maxLength,
} as const;

/** The fields whose change means every document must be re-ingested (`index.changeWarning`). */
export const SETTINGS_FIELDS = [
  "method",
  "methodParameters",
  "embeddingModel",
  "embeddingDimensions",
  "languageAnalyzer",
] as const;

/** One method version as a single choice. */
export function methodValue(key: string, version: number): string {
  return `${key}@${String(version)}`;
}

export function parseMethodValue(value: string): { key: string; version: number } {
  const at = value.lastIndexOf("@");
  return { key: value.slice(0, at), version: Number(value.slice(at + 1)) };
}

export function methodOf(options: IndexOptions, value: string): MethodOption | undefined {
  return options.methods.find((method) => methodValue(method.key, method.version) === value);
}

/** A method's choice text: its display name, with the version when its key has several. */
export function methodLabel(options: IndexOptions, method: MethodOption): string {
  const versions = options.methods.filter((each) => each.key === method.key).length;
  return versions > 1 ? `${method.displayName} (${String(method.version)})` : method.displayName;
}

/** The settings form of a method, or null when its schema leaves the supported subset. */
export function schemaOf(method: MethodOption | undefined): ParamSchema | null {
  return method === undefined ? null : parseParameterSchema(method.parameterSchema);
}

/** A method's starting settings: its `defaultParameters`, else each property's `default`. */
export function methodDefaults(method: MethodOption | undefined): ParamValues {
  const schema = schemaOf(method);
  return method === undefined || schema === null
    ? {}
    : initialParameters(schema, method.defaultParameters);
}

/** New index starts from `defaults` (section 7.7). */
export function defaultIndexValues(options: IndexOptions): IndexFormValues {
  const { defaults } = options;
  const method = methodValue(defaults.methodKey, defaults.methodVersion);
  return {
    displayName: "",
    description: "",
    method,
    methodParameters: methodDefaults(methodOf(options, method)),
    embeddingModel: defaults.embeddingModel,
    embeddingDimensions: defaults.embeddingDimensions,
    languageAnalyzer: defaults.languageAnalyzer,
  };
}

/** Edit settings starts from the index's own values. */
export function indexFormValues(index: IndexDetail): IndexFormValues {
  return {
    displayName: index.displayName,
    description: index.description ?? "",
    method: methodValue(index.method.key, index.method.version),
    methodParameters: index.method.parameters,
    embeddingModel: index.embedding.model,
    embeddingDimensions: index.embedding.dimensions,
    languageAnalyzer: index.languageAnalyzer,
  };
}

/** A new method takes its own default settings. */
export function withMethod(
  options: IndexOptions,
  values: IndexFormValues,
  method: string,
): IndexFormValues {
  return { ...values, method, methodParameters: methodDefaults(methodOf(options, method)) };
}

/** A new embedding model takes its `defaultDimensions`. */
export function withModel(
  options: IndexOptions,
  values: IndexFormValues,
  model: string,
): IndexFormValues {
  const option = options.embeddingModels.find((each) => each.model === model);
  return {
    ...values,
    embeddingModel: model,
    embeddingDimensions: option?.defaultDimensions ?? values.embeddingDimensions,
  };
}

/** Whether the draft changes a setting that needs a re-ingest. */
export function settingsChanged(base: IndexFormValues, draft: IndexFormValues): boolean {
  return SETTINGS_FIELDS.some((field) => !valuesEqual(base[field], draft[field]));
}

/** The dotted field names in the form's order, for focus on a failed submit. */
export function indexFieldOrder(schema: ParamSchema | null): string[] {
  return [
    "displayName",
    "description",
    "method",
    ...(schema?.properties.map((property) => parameterField(property.name)) ?? []),
    "embeddingModel",
    "embeddingDimensions",
    "languageAnalyzer",
  ];
}

/** The checks before Create index or Save changes (section 7.1). */
export function checkIndexForm(
  values: IndexFormValues,
  schema: ParamSchema | null,
): Map<string, string> {
  const messages = new Map<string, string>();
  const name = values.displayName.trim();
  if (name === "") messages.set("displayName", en["project.form.required"]);
  const nameTooLong = tooLongText(name, INDEX_LIMITS.displayName);
  if (nameTooLong !== null) messages.set("displayName", nameTooLong);
  const descriptionTooLong = tooLongText(values.description.trim(), INDEX_LIMITS.description);
  if (descriptionTooLong !== null) messages.set("description", descriptionTooLong);
  if (schema !== null) {
    for (const name of invalidParameters(schema, values.methodParameters)) {
      messages.set(parameterField(name), en["error.validation.field"]);
    }
  }
  return messages;
}

/** The screen's text for a `422` reason on an index field, or null for `error.validation.field`. */
export function indexReasonText(field: string, reason: string): string | null {
  if (field === "displayName" && (reason === "required" || reason === "too_short")) {
    return en["project.form.required"];
  }
  if (reason !== "too_long") return null;
  if (field === "displayName") return tooLongMessage(INDEX_LIMITS.displayName);
  if (field === "description") return tooLongMessage(INDEX_LIMITS.description);
  return null;
}

/** A `422` routed to the form: `body.methodKey` and `body.methodVersion` name the method field,
 * `body.methodParameters.<name>` that setting (section 7.7). */
export function routeIndexErrors(error: ApiError, schema: ParamSchema | null): RoutedFieldErrors {
  const renamed: ApiError = {
    ...error,
    errors: error.errors.map((entry) =>
      entry.path === "body.methodKey" || entry.path === "body.methodVersion"
        ? { ...entry, path: "body.method" }
        : entry,
    ),
  };
  return routeFieldErrors(renamed, indexFieldOrder(schema), indexReasonText);
}

/** `adminCreateIndex`'s body; an empty description is left out. */
export function createIndexBody(values: IndexFormValues, schema: ParamSchema | null): IndexCreate {
  const { key, version } = parseMethodValue(values.method);
  const description = values.description.trim();
  return {
    displayName: values.displayName.trim(),
    ...(description === "" ? {} : { description }),
    methodKey: key,
    methodVersion: version,
    methodParameters: {
      ...(schema === null ? {} : parametersToSend(schema, values.methodParameters)),
    },
    embeddingModel: values.embeddingModel,
    embeddingDimensions: values.embeddingDimensions,
    languageAnalyzer: values.languageAnalyzer,
  };
}

/** `adminUpdateIndex`'s body: only the fields that differ from `base` (section 7.4 step 2). A new
 * method sends its key and version; an emptied description clears it with `null`. */
export function updateIndexBody(
  base: IndexFormValues,
  draft: IndexFormValues,
  schema: ParamSchema | null,
): IndexUpdate {
  const changes = changesFrom(base, draft);
  const body: IndexUpdate = {};
  if (changes.displayName !== undefined) body.displayName = changes.displayName.trim();
  if (changes.description !== undefined) {
    const description = changes.description.trim();
    body.description = description === "" ? null : description;
  }
  if (changes.method !== undefined) {
    const { key, version } = parseMethodValue(changes.method);
    body.methodKey = key;
    body.methodVersion = version;
  }
  if (changes.methodParameters !== undefined) {
    body.methodParameters = {
      ...(schema === null
        ? changes.methodParameters
        : parametersToSend(schema, changes.methodParameters)),
    };
  }
  if (changes.embeddingModel !== undefined) body.embeddingModel = changes.embeddingModel;
  if (changes.embeddingDimensions !== undefined) {
    body.embeddingDimensions = changes.embeddingDimensions;
  }
  if (changes.languageAnalyzer !== undefined) body.languageAnalyzer = changes.languageAnalyzer;
  return body;
}

/** The fields a body fails in its generated schema. */
export function indexSchemaFields(body: IndexCreate | IndexUpdate, create: boolean): FieldMessages {
  const paths = schemaIssuePaths(create ? zIndexCreate : zIndexUpdate, body);
  return new Map(
    paths.map((path) => [
      path === "methodKey" || path === "methodVersion" ? "method" : path,
      en["error.validation.field"],
    ]),
  );
}
