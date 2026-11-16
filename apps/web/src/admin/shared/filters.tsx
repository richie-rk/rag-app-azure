// Admin list filters (10-web-client.md sections 6.1 and 7.1): a SearchBox with a 300 ms debounce
// and Dropdown filters, all mirrored to URL search parameters, so a reload or a shared link shows
// the same view. A parameter outside its allowed values is dropped with `replace` navigation.

import { Dropdown, Field, Option, SearchBox, makeStyles, tokens } from "@fluentui/react-components";
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useSearchParams } from "react-router";

import type { FilterText } from "../../copy/en";
import { SEARCH_DEBOUNCE_MS } from "../../platform/constants";

const useStyles = makeStyles({
  row: {
    display: "flex",
    flexWrap: "wrap",
    alignItems: "flex-end",
    columnGap: tokens.spacingHorizontalM,
    rowGap: tokens.spacingVerticalS,
  },
  search: { minWidth: "min(100%, 240px)" },
});

/** `value`, once it has stayed unchanged for `delayMs`. */
export function useDebouncedValue<T>(value: T, delayMs: number = SEARCH_DEBOUNCE_MS): T {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => {
      setSettled(value);
    }, delayMs);
    return () => {
      clearTimeout(timer);
    };
  }, [value, delayMs]);
  return settled;
}

/** One URL filter: its search parameter and the values it allows (any text when `allowed` is a
 * function returning true), with the value meant when it is absent. */
export interface UrlFilter {
  readonly name: string;
  readonly allowed: readonly string[] | ((value: string) => boolean);
  readonly fallback: string;
}

const CANONICAL_UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

/** `allowed` for a filter holding an ID, such as `?indexId=` or `?document=`: a canonical UUID. */
export function isCanonicalUuid(value: string): boolean {
  return CANONICAL_UUID.test(value);
}

function allows(filter: UrlFilter, value: string): boolean {
  return typeof filter.allowed === "function"
    ? filter.allowed(value)
    : filter.allowed.includes(value);
}

/** The filters' current values from the URL, and a setter that writes one back (removing it when
 * it equals its fallback). Invalid values are dropped from the URL with `replace`. */
export function useUrlFilters(filters: readonly UrlFilter[]) {
  const [params, setParams] = useSearchParams();

  const invalid = filters.filter((filter) => {
    const value = params.get(filter.name);
    return value !== null && !allows(filter, value);
  });
  const invalidNames = invalid.map((filter) => filter.name).join(",");

  useEffect(() => {
    if (invalidNames === "") return;
    setParams(
      (current) => {
        const next = new URLSearchParams(current);
        for (const name of invalidNames.split(",")) next.delete(name);
        return next;
      },
      { replace: true },
    );
  }, [invalidNames, setParams]);

  const values = useMemo(() => {
    const result: Record<string, string> = {};
    for (const filter of filters) {
      const value = params.get(filter.name);
      result[filter.name] = value !== null && allows(filter, value) ? value : filter.fallback;
    }
    return result;
  }, [filters, params]);

  const setFilter = useCallback(
    (name: string, value: string) => {
      const filter = filters.find((each) => each.name === name);
      setParams(
        (current) => {
          const next = new URLSearchParams(current);
          if (filter === undefined || value === filter.fallback || value === "") next.delete(name);
          else next.set(name, value);
          return next;
        },
        { replace: true },
      );
    },
    [filters, setParams],
  );

  return { values, setFilter };
}

export interface FilterSearchBoxProps {
  readonly label: string;
  /** The filter's value in the URL. */
  readonly value: string;
  /** Called 300 ms after typing stops. */
  readonly onSearch: (value: string) => void;
  /** The longest filter the API accepts, in code points; UTF-16 units never exceed it. */
  readonly maxLength: number;
}

/** The name filter: a SearchBox whose value reaches the URL 300 ms after typing stops. */
export function FilterSearchBox({ label, value, onSearch, maxLength }: FilterSearchBoxProps) {
  const styles = useStyles();
  const [text, setText] = useState(value);
  // A URL change from elsewhere (back, forward, a link) replaces the typed text.
  const [shown, setShown] = useState(value);
  if (shown !== value) {
    setShown(value);
    if (text.trim() !== value) setText(value);
  }
  const settled = useDebouncedValue(text);
  // The latest URL value and callback, read when the typed text settles; a URL change alone
  // (back, forward) must not write the URL again.
  const latest = useRef({ value, onSearch });
  useEffect(() => {
    latest.current = { value, onSearch };
  });
  useEffect(() => {
    const trimmed = settled.trim();
    if (trimmed !== latest.current.value) latest.current.onSearch(trimmed);
  }, [settled]);
  return (
    <Field label={label} className={styles.search}>
      <SearchBox
        value={text}
        maxLength={maxLength}
        onChange={(_, data) => {
          setText(data.value);
        }}
      />
    </Field>
  );
}

export interface FilterDropdownProps {
  readonly text: FilterText;
  /** The option values in order; their texts come from `text.options`. */
  readonly values: readonly string[];
  readonly value: string;
  readonly onChange: (value: string) => void;
}

/** A Dropdown filter whose choice is written to the URL. */
export function FilterDropdown({ text, values, value, onChange }: FilterDropdownProps) {
  return (
    <Field label={text.label}>
      <Dropdown
        value={text.options[value] ?? value}
        selectedOptions={[value]}
        onOptionSelect={(_, data) => {
          if (data.optionValue !== undefined) onChange(data.optionValue);
        }}
      >
        {values.map((option) => (
          <Option key={option} value={option}>
            {text.options[option] ?? option}
          </Option>
        ))}
      </Dropdown>
    </Field>
  );
}

/** The filter row above a table. */
export function FilterRow({ children }: { readonly children: ReactNode }) {
  const styles = useStyles();
  return <div className={styles.row}>{children}</div>;
}
