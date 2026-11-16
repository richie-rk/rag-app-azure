// One saved memory on the Memory screen (10-web-client.md section 7.3): its text, its category as
// a badge, when it was updated, and Edit and Delete; Edit swaps the text for an inline form with
// the fact box and the category, both prefilled, and Save and Cancel.

import {
  Badge,
  Button,
  Dropdown,
  Field,
  ListItem,
  Option,
  Spinner,
  Text,
  Textarea,
  makeStyles,
  tokens,
} from "@fluentui/react-components";
import { Delete20Regular, Edit20Regular } from "@fluentui/react-icons";
import { useEffect, useRef, type ReactNode } from "react";

import type { FactCategory } from "../api/generated/types.gen";
import type { ResponseOf } from "../api/operations";
import { displayNames, en } from "../copy/en";
import { fillText, formatDate } from "../ui/format";
import { FACT_CATEGORIES, keepCodePoints } from "./memoryText";

export type FactView = ResponseOf<"getMemory">["facts"][number];

const useStyles = makeStyles({
  item: {
    display: "flex",
    flexDirection: "column",
    rowGap: tokens.spacingVerticalS,
    paddingTop: tokens.spacingVerticalM,
    paddingBottom: tokens.spacingVerticalM,
    borderBottom: `${tokens.strokeWidthThin} solid ${tokens.colorNeutralStroke2}`,
  },
  text: { overflowWrap: "anywhere" },
  meta: {
    display: "flex",
    flexWrap: "wrap",
    alignItems: "center",
    columnGap: tokens.spacingHorizontalM,
    rowGap: tokens.spacingVerticalXS,
  },
  updated: { color: tokens.colorNeutralForeground3 },
  actions: { display: "flex", flexWrap: "wrap", columnGap: tokens.spacingHorizontalS },
  editor: { display: "flex", flexDirection: "column", rowGap: tokens.spacingVerticalM },
});

/** An open editor's typed values. */
export interface FactDraft {
  readonly factId: string;
  readonly text: string;
  readonly category: FactCategory;
}

/** Errors on the editor's two fields. */
export interface FactFieldErrors {
  readonly text?: string;
  readonly category?: string;
}

export interface FactItemProps {
  readonly fact: FactView;
  /** The open editor's values when this memory is being edited. */
  readonly draft: FactDraft | null;
  readonly fieldErrors: FactFieldErrors;
  readonly saving: boolean;
  readonly saveDisabled: boolean;
  readonly deleteDisabled: boolean;
  /** This memory's failed change, under it. */
  readonly failure: ReactNode;
  readonly onEdit: () => void;
  readonly onDraft: (draft: FactDraft) => void;
  readonly onSave: () => void;
  readonly onCancel: () => void;
  readonly onDelete: () => void;
}

export function FactItem({
  fact,
  draft,
  fieldErrors,
  saving,
  saveDisabled,
  deleteDisabled,
  failure,
  onEdit,
  onDraft,
  onSave,
  onCancel,
  onDelete,
}: FactItemProps) {
  const styles = useStyles();
  const textRef = useRef<HTMLTextAreaElement>(null);
  const editing = draft !== null;

  // Opening the editor puts the caret in the fact box.
  useEffect(() => {
    if (editing) textRef.current?.focus();
  }, [editing]);

  return (
    <ListItem className={styles.item}>
      {draft === null ? (
        <>
          <Text className={styles.text}>{fact.text}</Text>
          <div className={styles.meta}>
            <Badge appearance="tint" color="informative">
              {displayNames.factCategory[fact.category]}
            </Badge>
            <Text size={200} className={styles.updated}>
              {fillText(en["memory.updated"], { date: formatDate(fact.updatedAt) })}
            </Text>
            <span className={styles.actions}>
              <Button
                size="small"
                icon={<Edit20Regular />}
                aria-label={fillText(en["memory.editName"], { fact: fact.text })}
                data-fact-edit={fact.factId}
                onClick={onEdit}
              >
                {en["memory.edit"]}
              </Button>
              <Button
                size="small"
                icon={<Delete20Regular />}
                aria-label={fillText(en["memory.deleteName"], { fact: fact.text })}
                disabled={deleteDisabled}
                onClick={onDelete}
              >
                {en["memory.delete"]}
              </Button>
            </span>
          </div>
        </>
      ) : (
        <div className={styles.editor}>
          <Field
            label={en["memory.editLabel"]}
            {...(fieldErrors.text === undefined
              ? {}
              : { validationState: "error" as const, validationMessage: fieldErrors.text })}
          >
            <Textarea
              ref={(element: HTMLTextAreaElement | null) => {
                textRef.current = element;
              }}
              resize="vertical"
              value={draft.text}
              onChange={(_, data) => {
                onDraft({ ...draft, text: keepCodePoints(data.value) });
              }}
            />
          </Field>
          <Field
            label={en["memory.field.category"]}
            {...(fieldErrors.category === undefined
              ? {}
              : { validationState: "error" as const, validationMessage: fieldErrors.category })}
          >
            <Dropdown
              value={displayNames.factCategory[draft.category]}
              selectedOptions={[draft.category]}
              onOptionSelect={(_, data) => {
                const category = FACT_CATEGORIES.find((each) => each === data.optionValue);
                if (category !== undefined) onDraft({ ...draft, category });
              }}
            >
              {FACT_CATEGORIES.map((category) => (
                <Option key={category} value={category}>
                  {displayNames.factCategory[category]}
                </Option>
              ))}
            </Dropdown>
          </Field>
          <span className={styles.actions}>
            <Button
              appearance="primary"
              disabled={saving || saveDisabled}
              {...(saving ? { icon: <Spinner size="tiny" aria-hidden /> } : {})}
              onClick={onSave}
            >
              {en["memory.save"]}
            </Button>
            <Button onClick={onCancel} disabled={saving}>
              {en["memory.cancel"]}
            </Button>
          </span>
        </div>
      )}
      {failure}
    </ListItem>
  );
}
