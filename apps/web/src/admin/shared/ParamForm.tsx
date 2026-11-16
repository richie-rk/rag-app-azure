// The method settings form (10-web-client.md section 7.7): renders a method's `parameterSchema`
// for exactly the JSON Schema subset of `11-api.md` section 3.10.1 (schemas in `14-ingestion.md`
// section 9.3). A schema with any other keyword or type is unsupported: the screen shows
// `index.form.paramsUnsupported` and disables Create index and Save changes, so the form never sends
// a setting it can't show.

import {
  Dropdown,
  Input,
  Option,
  SpinButton,
  Switch,
  makeStyles,
  tokens,
} from "@fluentui/react-components";

import { en } from "../../copy/en";
import { codePointLength } from "../../ui/format";
import { AdminField } from "./AdminField";
import type { FieldMessages } from "./formErrors";

export interface ParamChoice {
  readonly value: string;
  /** The option's `title`, else the value. */
  readonly label: string;
}

interface ParamBase {
  readonly name: string;
  readonly title: string;
  readonly description: string | null;
  readonly defaultValue: unknown;
  readonly required: boolean;
}

export type ParamProperty = ParamBase &
  (
    | {
        readonly kind: "number";
        readonly integer: boolean;
        readonly minimum: number | null;
        readonly maximum: number | null;
        readonly multipleOf: number | null;
      }
    | { readonly kind: "boolean" }
    | { readonly kind: "choice"; readonly choices: readonly ParamChoice[] }
    | {
        readonly kind: "text";
        readonly minLength: number | null;
        readonly maxLength: number | null;
      }
    | {
        readonly kind: "choices";
        readonly choices: readonly ParamChoice[];
        readonly minItems: number | null;
        readonly maxItems: number | null;
      }
  );

export interface ParamSchema {
  readonly properties: readonly ParamProperty[];
}

export type ParamValues = Readonly<Record<string, unknown>>;

const ROOT_KEYS = new Set(["type", "properties", "required", "additionalProperties"]);
const ANNOTATIONS = ["title", "description", "default"];
const NUMBER_KEYS = new Set(["type", ...ANNOTATIONS, "minimum", "maximum", "multipleOf"]);
const BOOLEAN_KEYS = new Set(["type", ...ANNOTATIONS]);
const CHOICE_KEYS = new Set(["type", ...ANNOTATIONS, "enum", "oneOf"]);
const TEXT_KEYS = new Set(["type", ...ANNOTATIONS, "minLength", "maxLength"]);
const ARRAY_KEYS = new Set([
  "type",
  ...ANNOTATIONS,
  "items",
  "minItems",
  "maxItems",
  "uniqueItems",
]);
const ITEM_KEYS = new Set(["type", "enum", "oneOf"]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function onlyKeys(value: Record<string, unknown>, allowed: ReadonlySet<string>): boolean {
  return Object.keys(value).every((key) => allowed.has(key));
}

function optionalNumber(value: unknown): number | null | undefined {
  if (value === undefined) return null;
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function optionalCount(value: unknown): number | null | undefined {
  const count = optionalNumber(value);
  return count === undefined || (count !== null && (!Number.isInteger(count) || count < 0))
    ? undefined
    : count;
}

function optionalText(value: unknown): string | null | undefined {
  if (value === undefined) return null;
  return typeof value === "string" ? value : undefined;
}

/** The choices of an enumerated string: `enum` or `oneOf` of `{const, title}`, never both. */
function parseChoices(schema: Record<string, unknown>): ParamChoice[] | null {
  const { enum: values, oneOf } = schema;
  if (values !== undefined && oneOf !== undefined) return null;
  if (Array.isArray(values)) {
    if (values.length === 0 || !values.every((value) => typeof value === "string")) return null;
    return values.map((value) => ({ value, label: value }));
  }
  if (Array.isArray(oneOf)) {
    const choices: ParamChoice[] = [];
    for (const entry of oneOf) {
      if (!isRecord(entry) || !onlyKeys(entry, new Set(["const", "title"]))) return null;
      const { const: value, title } = entry;
      if (typeof value !== "string" || (title !== undefined && typeof title !== "string")) {
        return null;
      }
      choices.push({ value, label: title ?? value });
    }
    return choices.length === 0 ? null : choices;
  }
  return null;
}

function parseProperty(name: string, schema: unknown, required: boolean): ParamProperty | null {
  if (!isRecord(schema)) return null;
  const title = optionalText(schema["title"]);
  const description = optionalText(schema["description"]);
  if (title === undefined || description === undefined) return null;
  const base: ParamBase = {
    name,
    title: title ?? name,
    description,
    defaultValue: schema["default"],
    required,
  };
  switch (schema["type"]) {
    case "integer":
    case "number": {
      if (!onlyKeys(schema, NUMBER_KEYS)) return null;
      const minimum = optionalNumber(schema["minimum"]);
      const maximum = optionalNumber(schema["maximum"]);
      const multipleOf = optionalNumber(schema["multipleOf"]);
      if (minimum === undefined || maximum === undefined || multipleOf === undefined) return null;
      if (multipleOf !== null && multipleOf <= 0) return null;
      const integer = schema["type"] === "integer";
      return { ...base, kind: "number", integer, minimum, maximum, multipleOf };
    }
    case "boolean":
      return onlyKeys(schema, BOOLEAN_KEYS) ? { ...base, kind: "boolean" } : null;
    case "string": {
      if ("enum" in schema || "oneOf" in schema) {
        if (!onlyKeys(schema, CHOICE_KEYS)) return null;
        const choices = parseChoices(schema);
        return choices === null ? null : { ...base, kind: "choice", choices };
      }
      if (!onlyKeys(schema, TEXT_KEYS)) return null;
      const minLength = optionalCount(schema["minLength"]);
      const maxLength = optionalCount(schema["maxLength"]);
      if (minLength === undefined || maxLength === undefined) return null;
      return { ...base, kind: "text", minLength, maxLength };
    }
    case "array": {
      if (!onlyKeys(schema, ARRAY_KEYS)) return null;
      const items = schema["items"];
      if (!isRecord(items) || items["type"] !== "string" || !onlyKeys(items, ITEM_KEYS)) {
        return null;
      }
      const choices = parseChoices(items);
      const minItems = optionalCount(schema["minItems"]);
      const maxItems = optionalCount(schema["maxItems"]);
      const unique = schema["uniqueItems"];
      if (choices === null || minItems === undefined || maxItems === undefined) return null;
      if (unique !== undefined && unique !== true) return null;
      return { ...base, kind: "choices", choices, minItems, maxItems };
    }
    default:
      return null;
  }
}

/** The form a schema describes, or null when it leaves the supported subset. */
export function parseParameterSchema(schema: unknown): ParamSchema | null {
  if (!isRecord(schema) || !onlyKeys(schema, ROOT_KEYS)) return null;
  const { type, properties, required = [], additionalProperties } = schema;
  if (type !== "object" || additionalProperties !== false || !isRecord(properties)) return null;
  if (!Array.isArray(required) || !required.every((name) => typeof name === "string")) {
    return null;
  }
  const requiredNames = new Set<string>(required);
  if ([...requiredNames].some((name) => !(name in properties))) return null;
  const parsed: ParamProperty[] = [];
  for (const [name, property] of Object.entries(properties)) {
    const result = parseProperty(name, property, requiredNames.has(name));
    if (result === null) return null;
    parsed.push(result);
  }
  return { properties: parsed };
}

/** The starting values: the method's `defaultParameters`, else each property's `default`. */
export function initialParameters(schema: ParamSchema, defaults: ParamValues): ParamValues {
  const values: Record<string, unknown> = {};
  for (const property of schema.properties) {
    const value = property.name in defaults ? defaults[property.name] : property.defaultValue;
    if (value !== undefined) values[property.name] = value;
  }
  return values;
}

function isMultiple(value: number, step: number): boolean {
  const quotient = value / step;
  return Math.abs(quotient - Math.round(quotient)) < 1e-9;
}

/** Whether one value fits its property, as the API's parameter model would judge it. */
function isValid(property: ParamProperty, value: unknown): boolean {
  if (value === undefined || value === null || value === "") return !property.required;
  switch (property.kind) {
    case "number":
      return (
        typeof value === "number" &&
        Number.isFinite(value) &&
        (!property.integer || Number.isInteger(value)) &&
        (property.minimum === null || value >= property.minimum) &&
        (property.maximum === null || value <= property.maximum) &&
        (property.multipleOf === null || isMultiple(value, property.multipleOf))
      );
    case "boolean":
      return typeof value === "boolean";
    case "choice":
      return property.choices.some((choice) => choice.value === value);
    case "text": {
      if (typeof value !== "string") return false;
      const length = codePointLength(value);
      return (
        (property.minLength === null || length >= property.minLength) &&
        (property.maxLength === null || length <= property.maxLength)
      );
    }
    case "choices":
      return (
        Array.isArray(value) &&
        new Set(value).size === value.length &&
        value.every((item) => property.choices.some((choice) => choice.value === item)) &&
        (property.minItems === null || value.length >= property.minItems) &&
        (property.maxItems === null || value.length <= property.maxItems)
      );
  }
}

/** The names of the settings whose values don't fit their schema, in the form's order. */
export function invalidParameters(schema: ParamSchema, values: ParamValues): string[] {
  return schema.properties
    .filter((property) => !isValid(property, values[property.name]))
    .map((property) => property.name);
}

/** The values to send: only the schema's settings, empty ones left out. */
export function parametersToSend(schema: ParamSchema, values: ParamValues): ParamValues {
  const sent: Record<string, unknown> = {};
  for (const property of schema.properties) {
    const value = values[property.name];
    if (value !== undefined && value !== null && value !== "") sent[property.name] = value;
  }
  return sent;
}

/** A setting's value as text for a summary or a conflict line: choice titles, `projects.yes` or
 * `projects.no` for a switch. */
export function formatParameter(property: ParamProperty | undefined, value: unknown): string {
  const label = (item: unknown) =>
    property !== undefined && (property.kind === "choice" || property.kind === "choices")
      ? (property.choices.find((choice) => choice.value === item)?.label ?? String(item))
      : String(item);
  if (typeof value === "boolean") return value ? en["projects.yes"] : en["projects.no"];
  if (Array.isArray(value)) return value.map(label).join(", ");
  if (value === undefined || value === null || value === "") return en["admin.notSet"];
  return label(value);
}

/** Every setting as `title: value`, in the schema's order when the schema is known. */
export function describeParameters(
  schema: ParamSchema | null,
  values: ParamValues,
): { readonly name: string; readonly title: string; readonly value: string }[] {
  if (schema === null) {
    return Object.entries(values).map(([name, value]) => ({
      name,
      title: name,
      value: formatParameter(undefined, value),
    }));
  }
  return schema.properties.map((property) => ({
    name: property.name,
    title: property.title,
    value: formatParameter(property, values[property.name]),
  }));
}

const useStyles = makeStyles({
  root: { display: "flex", flexDirection: "column", rowGap: tokens.spacingVerticalM },
});

/** The field name a setting's errors use: `methodParameters.<name>` (section 7.1). */
export function parameterField(name: string): string {
  return `methodParameters.${name}`;
}

export interface ParamFormProps {
  readonly schema: ParamSchema;
  readonly values: ParamValues;
  readonly onChange: (values: ParamValues) => void;
  /** Errors by field name (`methodParameters.<name>`). */
  readonly errors: FieldMessages;
  readonly disabled?: boolean;
  /** Registers each control for focus on a failed submit, by field name. */
  readonly register?: (field: string) => (element: HTMLElement | null) => void;
}

function numberFrom(value: number | null | undefined, displayValue: string | undefined): unknown {
  if (value !== undefined) return value;
  const text = (displayValue ?? "").trim();
  if (text === "") return null;
  const parsed = Number(text);
  return Number.isNaN(parsed) ? text : parsed;
}

/** One control per setting: SpinButton, Switch, Dropdown, Input or a multi-select combobox. */
export function ParamForm({
  schema,
  values,
  onChange,
  errors,
  disabled = false,
  register,
}: ParamFormProps) {
  const styles = useStyles();
  const set = (name: string, value: unknown) => {
    onChange({ ...values, [name]: value });
  };
  return (
    <div className={styles.root}>
      {schema.properties.map((property) => {
        const field = parameterField(property.name);
        const value = values[property.name];
        const ref = register?.(field);
        const fieldProps = {
          label: property.title,
          required: property.required,
          error: errors.get(field) ?? null,
          ...(property.description === null ? {} : { hint: property.description }),
        };
        switch (property.kind) {
          case "number":
            return (
              <AdminField key={property.name} {...fieldProps}>
                <SpinButton
                  ref={ref}
                  disabled={disabled}
                  value={typeof value === "number" ? value : null}
                  {...(property.minimum === null ? {} : { min: property.minimum })}
                  {...(property.maximum === null ? {} : { max: property.maximum })}
                  {...(property.multipleOf === null ? {} : { step: property.multipleOf })}
                  onChange={(_, data) => {
                    set(property.name, numberFrom(data.value, data.displayValue));
                  }}
                />
              </AdminField>
            );
          case "boolean":
            return (
              <AdminField key={property.name} {...fieldProps}>
                <Switch
                  ref={ref}
                  disabled={disabled}
                  checked={value === true}
                  onChange={(_, data) => {
                    set(property.name, data.checked);
                  }}
                />
              </AdminField>
            );
          case "choice": {
            const selected = property.choices.find((choice) => choice.value === value);
            return (
              <AdminField key={property.name} {...fieldProps}>
                <Dropdown
                  ref={ref}
                  disabled={disabled}
                  value={selected?.label ?? ""}
                  selectedOptions={selected === undefined ? [] : [selected.value]}
                  onOptionSelect={(_, data) => {
                    set(property.name, data.optionValue);
                  }}
                >
                  {property.choices.map((choice) => (
                    <Option key={choice.value} value={choice.value}>
                      {choice.label}
                    </Option>
                  ))}
                </Dropdown>
              </AdminField>
            );
          }
          case "text":
            return (
              <AdminField key={property.name} {...fieldProps}>
                <Input
                  ref={ref}
                  disabled={disabled}
                  value={typeof value === "string" ? value : ""}
                  onChange={(_, data) => {
                    set(property.name, data.value);
                  }}
                />
              </AdminField>
            );
          case "choices": {
            const chosen = Array.isArray(value)
              ? value.filter((item): item is string => typeof item === "string")
              : [];
            return (
              <AdminField key={property.name} {...fieldProps}>
                {/* Fluent's select-only combobox: an editable multi-select Combobox shows no
                    chosen options in its box, so the choice would be invisible until opened. */}
                <Dropdown
                  ref={ref}
                  multiselect
                  disabled={disabled}
                  value={formatParameter(property, chosen)}
                  selectedOptions={chosen}
                  onOptionSelect={(_, data) => {
                    // Keep the schema's order, whatever order the options were picked in.
                    const picked = new Set(data.selectedOptions);
                    set(
                      property.name,
                      property.choices
                        .filter((choice) => picked.has(choice.value))
                        .map((choice) => choice.value),
                    );
                  }}
                >
                  {property.choices.map((choice) => (
                    <Option key={choice.value} value={choice.value}>
                      {choice.label}
                    </Option>
                  ))}
                </Dropdown>
              </AdminField>
            );
          }
        }
      })}
    </div>
  );
}
