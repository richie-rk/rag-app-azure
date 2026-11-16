// The index form's fields (10-web-client.md sections 7.7 and 7.8), shared by New index and Edit
// settings: Name, Description, Ingestion method with its description and hint, Method settings
// (the method's `parameterSchema` as a form), Embedding model, Vector dimensions and Language.

import {
  Dropdown,
  Input,
  Label,
  MessageBar,
  MessageBarBody,
  Option,
  Textarea,
  makeStyles,
  tokens,
} from "@fluentui/react-components";
import { useId, type ReactNode } from "react";

import { en } from "../../copy/en";
import { AdminField, ConflictLine, type FieldConflict } from "../shared/AdminField";
import type { FieldMessages } from "../shared/formErrors";
import { ParamForm, describeParameters, type ParamSchema } from "../shared/ParamForm";
import type { IndexOptions } from "../shared/queries";
import {
  methodLabel,
  methodOf,
  methodValue,
  schemaOf,
  withMethod,
  withModel,
  type IndexFormValues,
} from "./indexForm";

const useStyles = makeStyles({
  form: {
    display: "flex",
    flexDirection: "column",
    rowGap: tokens.spacingVerticalL,
    maxWidth: "640px",
  },
  group: { display: "flex", flexDirection: "column", rowGap: tokens.spacingVerticalS },
  hints: { display: "flex", flexDirection: "column", rowGap: tokens.spacingVerticalXXS },
});

type Field = keyof IndexFormValues;

/** A field's value as text, for `admin.conflict.latestValue` and the Index summary. */
export function indexValueText(
  options: IndexOptions | undefined,
  name: Field,
  value: unknown,
): string {
  if (name === "description")
    return typeof value === "string" && value !== "" ? value : en["admin.notSet"];
  if (name === "method" && options !== undefined && typeof value === "string") {
    const method = methodOf(options, value);
    return method === undefined ? value : methodLabel(options, method);
  }
  if (name === "languageAnalyzer" && options !== undefined) {
    return options.languageAnalyzers.find((each) => each.value === value)?.label ?? String(value);
  }
  if (name === "methodParameters" && typeof value === "object" && value !== null) {
    return describeParameters(null, value as Record<string, unknown>)
      .map((each) => `${each.title}: ${each.value}`)
      .join("; ");
  }
  return String(value);
}

export interface IndexFormProps {
  readonly options: IndexOptions;
  readonly values: IndexFormValues;
  readonly onChange: (values: IndexFormValues) => void;
  readonly errors: FieldMessages;
  readonly conflicts?: Partial<IndexFormValues>;
  readonly onUseLatest?: (name: Field) => void;
  readonly register: (field: string) => (element: HTMLElement | null) => void;
  /** Edit settings: `index.changeWarning` above Save when a setting changed. */
  readonly warning?: ReactNode;
  readonly actions: ReactNode;
}

export function IndexForm({
  options,
  values,
  onChange,
  errors,
  conflicts = {},
  onUseLatest,
  register,
  warning,
  actions,
}: IndexFormProps) {
  const styles = useStyles();
  const paramsLabel = useId();
  const method = methodOf(options, values.method);
  const schema: ParamSchema | null = schemaOf(method);
  const model = options.embeddingModels.find((each) => each.model === values.embeddingModel);
  const language = options.languageAnalyzers.find((each) => each.value === values.languageAnalyzer);
  const conflictOf = (name: Field): FieldConflict | null =>
    name in conflicts && onUseLatest !== undefined
      ? {
          value: indexValueText(options, name, conflicts[name]),
          onUseLatest: () => {
            onUseLatest(name);
          },
        }
      : null;
  const set = <K extends Field>(name: K, value: IndexFormValues[K]) => {
    onChange({ ...values, [name]: value });
  };
  const paramsConflict = conflictOf("methodParameters");

  return (
    // Not an HTML form: the app submits none (`form-action 'none'`); the buttons send it.
    <div className={styles.form}>
      <AdminField
        label={en["index.form.name"]}
        required
        error={errors.get("displayName") ?? null}
        conflict={conflictOf("displayName")}
      >
        <Input
          ref={register("displayName")}
          value={values.displayName}
          onChange={(_, data) => {
            set("displayName", data.value);
          }}
        />
      </AdminField>
      <AdminField
        label={en["index.form.description"]}
        error={errors.get("description") ?? null}
        conflict={conflictOf("description")}
      >
        <Textarea
          ref={register("description")}
          resize="vertical"
          value={values.description}
          onChange={(_, data) => {
            set("description", data.value);
          }}
        />
      </AdminField>
      <AdminField
        label={en["index.form.method"]}
        required
        error={errors.get("method") ?? null}
        conflict={conflictOf("method")}
        hint={
          <span className={styles.hints}>
            {method === undefined ? null : <span>{method.description}</span>}
            <span>{en["index.form.method.hint"]}</span>
          </span>
        }
      >
        <Dropdown
          ref={register("method")}
          value={method === undefined ? "" : methodLabel(options, method)}
          selectedOptions={[values.method]}
          onOptionSelect={(_, data) => {
            if (data.optionValue !== undefined) {
              onChange(withMethod(options, values, data.optionValue));
            }
          }}
        >
          {options.methods.map((each) => {
            const value = methodValue(each.key, each.version);
            return (
              <Option key={value} value={value}>
                {methodLabel(options, each)}
              </Option>
            );
          })}
        </Dropdown>
      </AdminField>
      <div role="group" aria-labelledby={paramsLabel} className={styles.group}>
        <Label id={paramsLabel}>{en["index.form.params"]}</Label>
        {schema === null ? (
          <MessageBar intent="warning" layout="multiline">
            <MessageBarBody>{en["index.form.paramsUnsupported"]}</MessageBarBody>
          </MessageBar>
        ) : (
          <ParamForm
            schema={schema}
            values={values.methodParameters}
            errors={errors}
            register={register}
            onChange={(next) => {
              set("methodParameters", next);
            }}
          />
        )}
        {paramsConflict === null ? null : <ConflictLine conflict={paramsConflict} />}
      </div>
      <AdminField
        label={en["index.form.embedding"]}
        required
        error={errors.get("embeddingModel") ?? null}
        conflict={conflictOf("embeddingModel")}
      >
        <Dropdown
          ref={register("embeddingModel")}
          value={values.embeddingModel}
          selectedOptions={[values.embeddingModel]}
          onOptionSelect={(_, data) => {
            if (data.optionValue !== undefined) {
              onChange(withModel(options, values, data.optionValue));
            }
          }}
        >
          {options.embeddingModels.map((each) => (
            <Option key={each.model} value={each.model}>
              {each.model}
            </Option>
          ))}
        </Dropdown>
      </AdminField>
      <AdminField
        label={en["index.form.dimensions"]}
        hint={en["index.form.dimensions.hint"]}
        required
        error={errors.get("embeddingDimensions") ?? null}
        conflict={conflictOf("embeddingDimensions")}
      >
        <Dropdown
          ref={register("embeddingDimensions")}
          value={String(values.embeddingDimensions)}
          selectedOptions={[String(values.embeddingDimensions)]}
          onOptionSelect={(_, data) => {
            if (data.optionValue !== undefined) {
              set("embeddingDimensions", Number(data.optionValue));
            }
          }}
        >
          {(model?.dimensions ?? [values.embeddingDimensions]).map((each) => (
            <Option key={each} value={String(each)}>
              {String(each)}
            </Option>
          ))}
        </Dropdown>
      </AdminField>
      <AdminField
        label={en["index.form.language"]}
        hint={en["index.form.language.hint"]}
        required
        error={errors.get("languageAnalyzer") ?? null}
        conflict={conflictOf("languageAnalyzer")}
      >
        <Dropdown
          ref={register("languageAnalyzer")}
          value={language?.label ?? values.languageAnalyzer}
          selectedOptions={[values.languageAnalyzer]}
          onOptionSelect={(_, data) => {
            if (data.optionValue !== undefined) set("languageAnalyzer", data.optionValue);
          }}
        >
          {options.languageAnalyzers.map((each) => (
            <Option key={each.value} value={each.value}>
              {each.label}
            </Option>
          ))}
        </Dropdown>
      </AdminField>
      {warning === undefined ? null : warning}
      {actions}
    </div>
  );
}

/** Whether the form can be sent: the method's settings schema is supported. */
export function canSendIndexForm(options: IndexOptions, values: IndexFormValues): boolean {
  return schemaOf(methodOf(options, values.method)) !== null;
}

/** `index.changeWarning`, above Save on Edit settings. */
export function ChangeWarning() {
  return (
    <MessageBar intent="warning" layout="multiline">
      <MessageBarBody>{en["index.changeWarning"]}</MessageBarBody>
    </MessageBar>
  );
}
