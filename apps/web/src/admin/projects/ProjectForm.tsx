// The project form's fields (10-web-client.md section 7.6), shared by New project and the Details
// tab: Name, Description, Example questions, Instructions for answers and Add new users
// automatically, each a Fluent Field with its label, hint, error and, after a `412`, its latest
// saved value.

import {
  Button,
  Field,
  Input,
  Label,
  Switch,
  Textarea,
  makeStyles,
  tokens,
} from "@fluentui/react-components";
import { Add20Regular, Dismiss20Regular } from "@fluentui/react-icons";
import { useId, type ReactNode } from "react";

import { en } from "../../copy/en";
import { fillText } from "../../ui/format";
import { AdminField, ConflictLine, type FieldConflict } from "../shared/AdminField";
import type { FieldMessages } from "../shared/formErrors";
import { PROJECT_LIMITS, type ProjectFormValues } from "./projectForm";

const useStyles = makeStyles({
  form: {
    display: "flex",
    flexDirection: "column",
    rowGap: tokens.spacingVerticalL,
    maxWidth: "640px",
  },
  examples: { display: "flex", flexDirection: "column", rowGap: tokens.spacingVerticalS },
  example: { display: "flex", alignItems: "flex-start", columnGap: tokens.spacingHorizontalS },
  exampleField: { flexGrow: 1 },
  hint: { color: tokens.colorNeutralForeground3, fontSize: tokens.fontSizeBase200 },
});

type Field = keyof ProjectFormValues;

/** A field's latest saved value as text, for `admin.conflict.latestValue`. */
export function projectValueText(name: Field, value: unknown): string {
  if (name === "isDefault") return value === true ? en["projects.yes"] : en["projects.no"];
  if (name === "exampleQuestions") {
    const questions = Array.isArray(value) ? value.map(String) : [];
    return questions.length === 0 ? en["admin.notSet"] : questions.join("; ");
  }
  return typeof value === "string" && value !== "" ? value : en["admin.notSet"];
}

export interface ProjectFormProps {
  readonly values: ProjectFormValues;
  readonly onChange: <K extends Field>(name: K, value: ProjectFormValues[K]) => void;
  readonly errors: FieldMessages;
  /** After a `412`: the latest saved value of each field another change touched. */
  readonly conflicts?: Partial<ProjectFormValues>;
  readonly onUseLatest?: (name: Field) => void;
  readonly register: (field: string) => (element: HTMLElement | null) => void;
  /** New project's Members picker, the first field. */
  readonly members?: ReactNode;
  /** The form's actions: Create project or Save changes, and Cancel. */
  readonly actions: ReactNode;
}

export function ProjectForm({
  values,
  onChange,
  errors,
  conflicts = {},
  onUseLatest,
  register,
  members,
  actions,
}: ProjectFormProps) {
  const styles = useStyles();
  const examplesId = useId();
  const conflictOf = (name: Field): FieldConflict | null =>
    name in conflicts && onUseLatest !== undefined
      ? {
          value: projectValueText(name, conflicts[name]),
          onUseLatest: () => {
            onUseLatest(name);
          },
        }
      : null;
  const examples = values.exampleQuestions;
  const examplesConflict = conflictOf("exampleQuestions");

  return (
    // Not an HTML form: the app submits none (`form-action 'none'`); Save and Create send it.
    <div className={styles.form}>
      {members}
      <AdminField
        label={en["project.form.name"]}
        required
        error={errors.get("name") ?? null}
        conflict={conflictOf("name")}
      >
        <Input
          ref={register("name")}
          value={values.name}
          onChange={(_, data) => {
            onChange("name", data.value);
          }}
        />
      </AdminField>
      <AdminField
        label={en["project.form.description"]}
        hint={en["project.form.description.hint"]}
        error={errors.get("description") ?? null}
        conflict={conflictOf("description")}
      >
        <Input
          ref={register("description")}
          value={values.description}
          onChange={(_, data) => {
            onChange("description", data.value);
          }}
        />
      </AdminField>
      <div role="group" aria-labelledby={examplesId} className={styles.examples}>
        <Label id={examplesId}>{en["project.form.examples"]}</Label>
        <span className={styles.hint}>
          {fillText(en["project.form.examples.hint"], { max: PROJECT_LIMITS.exampleQuestions })}
        </span>
        {examples.map((question, row) => {
          const n = row + 1;
          const field = `exampleQuestions.${String(row)}`;
          const error = errors.get(field);
          return (
            <div key={row} className={styles.example}>
              <Field
                className={styles.exampleField}
                label={fillText(en["project.form.examples.item"], { n })}
                {...(error === undefined
                  ? {}
                  : { validationState: "error" as const, validationMessage: error })}
              >
                <Input
                  ref={register(field)}
                  value={question}
                  onChange={(_, data) => {
                    onChange(
                      "exampleQuestions",
                      examples.map((each, index) => (index === row ? data.value : each)),
                    );
                  }}
                />
              </Field>
              <Button
                appearance="subtle"
                icon={<Dismiss20Regular />}
                aria-label={fillText(en["project.form.examples.remove"], { n })}
                onClick={() => {
                  onChange(
                    "exampleQuestions",
                    examples.filter((_, index) => index !== row),
                  );
                }}
              />
            </div>
          );
        })}
        <div>
          <Button
            icon={<Add20Regular />}
            disabled={examples.length >= PROJECT_LIMITS.exampleQuestions}
            onClick={() => {
              onChange("exampleQuestions", [...examples, ""]);
            }}
          >
            {en["project.form.examples.add"]}
          </Button>
        </div>
        {examplesConflict === null ? null : <ConflictLine conflict={examplesConflict} />}
      </div>
      <AdminField
        label={en["project.form.instructions"]}
        hint={en["project.form.instructions.hint"]}
        error={errors.get("instructions") ?? null}
        conflict={conflictOf("instructions")}
      >
        <Textarea
          ref={register("instructions")}
          resize="vertical"
          value={values.instructions}
          onChange={(_, data) => {
            onChange("instructions", data.value);
          }}
        />
      </AdminField>
      <AdminField
        label={en["project.form.joinNewUsers"]}
        hint={en["project.form.joinNewUsers.hint"]}
        error={errors.get("isDefault") ?? null}
        conflict={conflictOf("isDefault")}
      >
        <Switch
          ref={register("isDefault")}
          checked={values.isDefault}
          onChange={(_, data) => {
            onChange("isDefault", data.checked);
          }}
        />
      </AdminField>
      {actions}
    </div>
  );
}
