// The people picker (10-web-client.md section 7.6): a Fluent TagPicker over `adminSearchUsers`
// that searches from 2 characters with a 300 ms debounce and `limit=20`. Only people who have
// signed in at least once can be found. With a project, current members come back with
// `isMember: true` and show disabled.

import {
  Avatar,
  Field,
  Spinner,
  Tag,
  TagPicker,
  TagPickerControl,
  TagPickerGroup,
  TagPickerInput,
  TagPickerList,
  TagPickerOption,
  Text,
  makeStyles,
  tokens,
  type FieldProps,
} from "@fluentui/react-components";
import { useQuery } from "@tanstack/react-query";
import { useState, type Ref } from "react";

import { apiErrorOf } from "../../api/problem";
import { en } from "../../copy/en";
import { SEARCH_MIN_CHARS } from "../../platform/constants";
import { ApiErrorBar } from "../../ui/ErrorBar";
import { codePointLength } from "../../ui/format";
import { useDebouncedValue } from "./filters";
import { userSearchQuery, type UserSummary } from "./queries";

const useStyles = makeStyles({
  root: { display: "flex", flexDirection: "column", rowGap: tokens.spacingVerticalXS },
  status: { color: tokens.colorNeutralForeground3 },
});

/** The name a person goes by on screen: the display name, else the e-mail address. */
export function personName(person: Pick<UserSummary, "displayName" | "email">): string {
  return person.displayName === "" ? person.email : person.displayName;
}

export interface PeoplePickerProps {
  readonly label: string;
  readonly hint?: FieldProps["hint"];
  readonly placeholder: string;
  /** Adding to this project: its members show disabled. */
  readonly projectId?: string;
  /** The chosen people, in the order chosen. */
  readonly selected: readonly UserSummary[];
  readonly onChange: (selected: UserSummary[]) => void;
  /** While a batch runs the picker is read-only. */
  readonly disabled?: boolean;
  /** The field's error, such as `project.form.members.notFound`. */
  readonly error?: string | null;
  /** At most this many people can be chosen. */
  readonly maxPeople?: number;
  /** The picker's root, for focus on a failed submit (its search box takes it). */
  readonly rootRef?: Ref<HTMLDivElement>;
}

export function PeoplePicker({
  label,
  hint,
  placeholder,
  projectId,
  selected,
  onChange,
  disabled = false,
  error = null,
  maxPeople,
  rootRef,
}: PeoplePickerProps) {
  const styles = useStyles();
  const [text, setText] = useState("");
  const q = useDebouncedValue(text.trim());
  const searchable = codePointLength(q) >= SEARCH_MIN_CHARS;
  const search = useQuery({ ...userSearchQuery(q, projectId), enabled: searchable });
  const chosenIds = new Set(selected.map((person) => person.userId));
  const full = maxPeople !== undefined && selected.length >= maxPeople;
  const results = searchable ? (search.data?.items ?? []) : [];
  const options = results.filter((person) => !chosenIds.has(person.userId));

  const select = (userId: string) => {
    if (chosenIds.has(userId)) {
      onChange(selected.filter((person) => person.userId !== userId));
      return;
    }
    const person = results.find((each) => each.userId === userId);
    if (person === undefined || person.isMember === true || full) return;
    onChange([...selected, person]);
    setText("");
  };

  let status: string | null = null;
  if (text.trim() !== "" && codePointLength(text.trim()) < SEARCH_MIN_CHARS) {
    status = en["members.picker.minChars"];
  } else if (searchable && search.isSuccess && results.length === 0) {
    status = en["members.picker.none"];
  }
  const searchError = searchable && search.isError ? apiErrorOf(search.error) : null;

  return (
    <div className={styles.root} ref={rootRef}>
      <Field
        label={label}
        {...(hint === undefined ? {} : { hint })}
        {...(error === null ? {} : { validationState: "error" as const, validationMessage: error })}
      >
        <TagPicker
          disabled={disabled}
          // With no option to offer, the picker's only child is its control, not a popover.
          noPopover={options.length === 0}
          selectedOptions={selected.map((person) => person.userId)}
          onOptionSelect={(_, data) => {
            select(data.value);
          }}
        >
          <TagPickerControl
            {...(searchable && search.isFetching
              ? { secondaryAction: <Spinner size="extra-tiny" aria-label={label} /> }
              : {})}
          >
            <TagPickerGroup aria-label={label}>
              {selected.map((person) => (
                <Tag
                  key={person.userId}
                  value={person.userId}
                  shape="rounded"
                  media={<Avatar name={personName(person)} size={20} aria-hidden="true" />}
                >
                  {personName(person)}
                </Tag>
              ))}
            </TagPickerGroup>
            <TagPickerInput
              aria-label={label}
              placeholder={placeholder}
              value={text}
              onChange={(event) => {
                setText(event.target.value);
              }}
            />
          </TagPickerControl>
          {options.length === 0 ? undefined : (
            <TagPickerList>
              {options.map((person) => (
                <TagPickerOption
                  key={person.userId}
                  value={person.userId}
                  text={personName(person)}
                  secondaryContent={person.email}
                  media={<Avatar name={personName(person)} size={24} aria-hidden="true" />}
                  // The option is a Fluent Option underneath and honours `disabled`, which its
                  // props type leaves out.
                  {...{ disabled: person.isMember === true || full }}
                >
                  {personName(person)}
                </TagPickerOption>
              ))}
            </TagPickerList>
          )}
        </TagPicker>
      </Field>
      {status === null ? null : (
        <Text size={200} className={styles.status}>
          {status}
        </Text>
      )}
      {searchError === null ? null : (
        <ApiErrorBar
          error={searchError}
          onRetry={() => {
            void search.refetch();
          }}
        />
      )}
    </div>
  );
}
