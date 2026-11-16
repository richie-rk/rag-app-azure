// One admin form field (10-web-client.md sections 7.1 and 7.4): a Fluent Field with its visible
// label, its hint, its error (`validationState="error"`) and, after a `412`, the latest saved value
// with "Use latest value".

import { Field, Link, Text, makeStyles, tokens, type FieldProps } from "@fluentui/react-components";
import type { ReactElement } from "react";

import { en } from "../../copy/en";
import { fillText } from "../../ui/format";

const useStyles = makeStyles({
  root: { display: "flex", flexDirection: "column", rowGap: tokens.spacingVerticalXS },
  conflict: {
    display: "flex",
    flexWrap: "wrap",
    alignItems: "baseline",
    columnGap: tokens.spacingHorizontalS,
  },
});

/** The latest saved value of a field another change touched, and how to take it. */
export interface FieldConflict {
  /** The value as text. */
  readonly value: string;
  readonly onUseLatest: () => void;
}

export interface AdminFieldProps {
  readonly label: string;
  readonly hint?: FieldProps["hint"];
  readonly required?: boolean;
  /** The field's error, or null. */
  readonly error?: string | null;
  readonly conflict?: FieldConflict | null;
  readonly children: ReactElement;
}

/** The conflict line under a field: `admin.conflict.latestValue` and `admin.conflict.useLatest`. */
export function ConflictLine({ conflict }: { readonly conflict: FieldConflict }) {
  const styles = useStyles();
  return (
    <div className={styles.conflict}>
      <Text size={200}>
        {fillText(en["admin.conflict.latestValue"], { value: conflict.value })}
      </Text>
      <Link as="button" onClick={conflict.onUseLatest}>
        {en["admin.conflict.useLatest"]}
      </Link>
    </div>
  );
}

export function AdminField({
  label,
  hint,
  required = false,
  error = null,
  conflict = null,
  children,
}: AdminFieldProps) {
  const styles = useStyles();
  return (
    <div className={styles.root}>
      <Field
        label={label}
        required={required}
        {...(hint === undefined ? {} : { hint })}
        {...(error === null ? {} : { validationState: "error" as const, validationMessage: error })}
      >
        {children}
      </Field>
      {conflict === null ? null : <ConflictLine conflict={conflict} />}
    </div>
  );
}
