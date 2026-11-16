// Admin forms and edit conflicts (10-web-client.md section 7.4, ADR 0094), implemented once for the
// project Details form and the index settings form:
//
// 1. the form loads the resource as `base` and keeps its `rowVersion`;
// 2. the user edits a `draft`; Save sends only the fields that differ from `base`, with `If-Match`;
// 3. `200`: the response becomes `base` (and replaces the cache);
// 4. `412`: the resource is read once more (`latest`); the draft stays; every field where `latest`
//    differs from `base` shows its saved value with "Use latest value"; `base` becomes `latest`, so
//    the next Save follows step 3 with the new `rowVersion`.
//
// A membership or mapping change moves the kept `rowVersion` (section 5.6 step 4) without touching
// the loaded values or the draft, because members and mappings aren't form fields.

import { useCallback, useMemo, useReducer } from "react";

/** A form's values: plain JSON data (strings, numbers, booleans, arrays and objects of them). */
export type FormValues = object;

/** Whether two JSON values are equal, arrays and objects compared member by member. */
export function valuesEqual(a: unknown, b: unknown): boolean {
  if (Object.is(a, b)) return true;
  if (Array.isArray(a) || Array.isArray(b)) {
    return (
      Array.isArray(a) &&
      Array.isArray(b) &&
      a.length === b.length &&
      a.every((item, index) => valuesEqual(item, b[index]))
    );
  }
  if (typeof a !== "object" || typeof b !== "object" || a === null || b === null) return false;
  const left = a as Record<string, unknown>;
  const right = b as Record<string, unknown>;
  const keys = new Set([...Object.keys(left), ...Object.keys(right)]);
  return [...keys].every((key) => valuesEqual(left[key], right[key]));
}

/** The names of the fields whose values differ between `from` and `to`, in `to`'s order. */
export function changedFields<V extends FormValues>(from: V, to: V): (keyof V & string)[] {
  return (Object.keys(to) as (keyof V & string)[]).filter(
    (name) => !valuesEqual(from[name], to[name]),
  );
}

/** Step 2: the draft's fields that differ from `base`, the only ones Save sends. */
export function changesFrom<V extends FormValues>(base: V, draft: V): Partial<V> {
  const changes: Partial<V> = {};
  for (const name of changedFields(base, draft)) changes[name] = draft[name];
  return changes;
}

export interface EditFormState<V extends FormValues> {
  /** The values the form last loaded or saved. */
  readonly base: V;
  /** The version Save sends as `If-Match`. */
  readonly rowVersion: string;
  readonly draft: V;
  /** After a `412`: the latest saved value of each field where it differed from `base`. */
  readonly latest: Partial<V>;
}

export type EditFormAction<V extends FormValues> =
  | { readonly type: "edit"; readonly name: keyof V; readonly value: unknown }
  | { readonly type: "replaceDraft"; readonly draft: V }
  | { readonly type: "saved"; readonly values: V; readonly rowVersion: string }
  | { readonly type: "conflict"; readonly values: V; readonly rowVersion: string }
  | { readonly type: "takeLatest"; readonly name: keyof V }
  | { readonly type: "moveRowVersion"; readonly sent: string; readonly next: string }
  | { readonly type: "reload"; readonly values: V; readonly rowVersion: string };

export function initialEditForm<V extends FormValues>(
  values: V,
  rowVersion: string,
): EditFormState<V> {
  return { base: values, rowVersion, draft: values, latest: {} };
}

export function editFormReducer<V extends FormValues>(
  state: EditFormState<V>,
  action: EditFormAction<V>,
): EditFormState<V> {
  switch (action.type) {
    case "edit":
      return { ...state, draft: { ...state.draft, [action.name]: action.value } };
    case "replaceDraft":
      return { ...state, draft: action.draft };
    case "saved":
      // Step 3: the response becomes base and the draft; no conflict remains.
      return initialEditForm(action.values, action.rowVersion);
    case "conflict": {
      // Step 4: the draft stays; fields the other change touched show their saved value.
      const latest: Partial<V> = {};
      for (const name of changedFields(state.base, action.values)) {
        latest[name] = action.values[name];
      }
      return { base: action.values, rowVersion: action.rowVersion, draft: state.draft, latest };
    }
    case "takeLatest": {
      if (!(action.name in state.latest)) return state;
      const value = state.latest[action.name];
      const latest = Object.fromEntries(
        Object.entries(state.latest).filter(([name]) => name !== action.name),
      ) as Partial<V>;
      return { ...state, draft: { ...state.draft, [action.name]: value }, latest };
    }
    case "moveRowVersion":
      // Section 5.6 step 4: only when the change was sent with the version this form keeps.
      return state.rowVersion === action.sent ? { ...state, rowVersion: action.next } : state;
    case "reload":
      // Loading fresh data is only safe while the user has nothing to lose.
      return valuesEqual(state.base, state.draft) && Object.keys(state.latest).length === 0
        ? initialEditForm(action.values, action.rowVersion)
        : state;
  }
}

export interface EditForm<V extends FormValues> extends EditFormState<V> {
  /** The fields Save sends: those that differ from `base`. */
  readonly changes: Partial<V>;
  readonly isDirty: boolean;
  /** The latest saved values still worth showing: those the draft doesn't already hold. */
  readonly conflicts: Partial<V>;
  readonly setField: <K extends keyof V>(name: K, value: V[K]) => void;
  readonly replaceDraft: (draft: V) => void;
  /** Step 3, after a `200`. */
  readonly saved: (values: V, rowVersion: string) => void;
  /** Step 4, after a `412` and the read of `latest`. */
  readonly conflict: (values: V, rowVersion: string) => void;
  /** "Use latest value": copies the field's latest saved value into the draft. */
  readonly takeLatest: (name: keyof V) => void;
  /** Section 5.6 step 4: a membership or mapping change sent `sent` and returned `next`. */
  readonly moveRowVersion: (sent: string, next: string) => void;
  /** Newer data arrived elsewhere; it replaces the form only while nothing was typed. */
  readonly reload: (values: V, rowVersion: string) => void;
}

export function useEditForm<V extends FormValues>(values: V, rowVersion: string): EditForm<V> {
  const [state, dispatch] = useReducer(editFormReducer<V>, undefined, (): EditFormState<V> =>
    initialEditForm(values, rowVersion),
  );
  const setField = useCallback(<K extends keyof V>(name: K, value: V[K]) => {
    dispatch({ type: "edit", name, value });
  }, []);
  const replaceDraft = useCallback((draft: V) => {
    dispatch({ type: "replaceDraft", draft });
  }, []);
  const saved = useCallback((next: V, version: string) => {
    dispatch({ type: "saved", values: next, rowVersion: version });
  }, []);
  const conflict = useCallback((next: V, version: string) => {
    dispatch({ type: "conflict", values: next, rowVersion: version });
  }, []);
  const takeLatest = useCallback((name: keyof V) => {
    dispatch({ type: "takeLatest", name });
  }, []);
  const moveRowVersion = useCallback((sent: string, next: string) => {
    dispatch({ type: "moveRowVersion", sent, next });
  }, []);
  const reload = useCallback((next: V, version: string) => {
    dispatch({ type: "reload", values: next, rowVersion: version });
  }, []);
  return useMemo(() => {
    const changes = changesFrom(state.base, state.draft);
    const conflicts: Partial<V> = {};
    for (const name of Object.keys(state.latest) as (keyof V)[]) {
      if (!valuesEqual(state.latest[name], state.draft[name])) {
        conflicts[name] = state.latest[name];
      }
    }
    return {
      ...state,
      changes,
      isDirty: Object.keys(changes).length > 0,
      conflicts,
      setField,
      replaceDraft,
      saved,
      conflict,
      takeLatest,
      moveRowVersion,
      reload,
    };
  }, [state, setField, replaceDraft, saved, conflict, takeLatest, moveRowVersion, reload]);
}
