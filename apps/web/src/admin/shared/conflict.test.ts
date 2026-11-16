import { act, renderHook } from "@testing-library/react";
import { describe, expect, test } from "vitest";

import {
  changedFields,
  changesFrom,
  editFormReducer,
  initialEditForm,
  useEditForm,
  valuesEqual,
} from "./conflict";

interface Values {
  readonly name: string;
  readonly questions: readonly string[];
  readonly on: boolean;
}

const BASE: Values = { name: "Alpha", questions: ["a"], on: false };

describe("the edit-conflict steps of section 7.4", () => {
  test("values compare member by member", () => {
    expect(valuesEqual(["a", { b: 1 }], ["a", { b: 1 }])).toBe(true);
    expect(valuesEqual(["a"], ["a", "b"])).toBe(false);
    expect(valuesEqual({ a: 1 }, { a: 1, b: undefined })).toBe(true);
    expect(valuesEqual({ a: 1 }, { a: 2 })).toBe(false);
    expect(valuesEqual(null, {})).toBe(false);
    expect(valuesEqual([1], { 0: 1 })).toBe(false);
  });

  test("step 2: Save sends only the fields that differ from base", () => {
    const draft = { ...BASE, name: "Beta" };
    expect(changedFields(BASE, draft)).toEqual(["name"]);
    expect(changesFrom(BASE, draft)).toEqual({ name: "Beta" });
    expect(changesFrom(BASE, BASE)).toEqual({});
  });

  test("step 4: a 412 keeps the draft, shows the latest value of each changed field, and moves base", () => {
    let state = initialEditForm(BASE, "AAAAAAAA001=");
    state = editFormReducer(state, { type: "edit", name: "name", value: "Typed" });
    const latest: Values = { name: "Theirs", questions: ["a", "b"], on: false };
    state = editFormReducer(state, {
      type: "conflict",
      values: latest,
      rowVersion: "AAAAAAAA002=",
    });
    expect(state.draft).toEqual({ ...BASE, name: "Typed" });
    expect(state.base).toBe(latest);
    expect(state.rowVersion).toBe("AAAAAAAA002=");
    expect(state.latest).toEqual({ name: "Theirs", questions: ["a", "b"] });
    // The next Save replaces the latest values with the draft's.
    expect(changesFrom(state.base, state.draft)).toEqual({ name: "Typed", questions: ["a"] });
    state = editFormReducer(state, { type: "takeLatest", name: "questions" });
    expect(state.draft.questions).toEqual(["a", "b"]);
    expect(state.latest).toEqual({ name: "Theirs" });
    expect(editFormReducer(state, { type: "takeLatest", name: "on" })).toBe(state);
  });

  test("step 3: a 200 makes the answer base and the draft", () => {
    let state = initialEditForm(BASE, "AAAAAAAA001=");
    state = editFormReducer(state, { type: "edit", name: "on", value: true });
    const saved = { ...BASE, on: true };
    state = editFormReducer(state, { type: "saved", values: saved, rowVersion: "AAAAAAAA003=" });
    expect(state).toEqual({ base: saved, draft: saved, rowVersion: "AAAAAAAA003=", latest: {} });
  });

  test("section 5.6 step 4: a member change moves the kept version only when it sent that version", () => {
    const state = editFormReducer(initialEditForm(BASE, "AAAAAAAA001="), {
      type: "edit",
      name: "name",
      value: "Typed",
    });
    const moved = editFormReducer(state, {
      type: "moveRowVersion",
      sent: "AAAAAAAA001=",
      next: "AAAAAAAA002=",
    });
    expect(moved.rowVersion).toBe("AAAAAAAA002=");
    expect(moved.draft.name).toBe("Typed");
    expect(moved.base).toBe(BASE);
    expect(
      editFormReducer(state, { type: "moveRowVersion", sent: "AAAAAAAA009=", next: "x" }),
    ).toBe(state);
  });

  test("newer data replaces the form only while nothing was typed", () => {
    const pristine = initialEditForm(BASE, "AAAAAAAA001=");
    const newer = { ...BASE, name: "Newer" };
    expect(
      editFormReducer(pristine, { type: "reload", values: newer, rowVersion: "AAAAAAAA002=" }),
    ).toEqual(initialEditForm(newer, "AAAAAAAA002="));
    const dirty = editFormReducer(pristine, { type: "replaceDraft", draft: { ...BASE, on: true } });
    expect(
      editFormReducer(dirty, { type: "reload", values: newer, rowVersion: "AAAAAAAA002=" }),
    ).toBe(dirty);
  });

  test("the hook exposes the changes, the dirty flag and the conflicts the draft doesn't hold", () => {
    const { result } = renderHook(() => useEditForm(BASE, "AAAAAAAA001="));
    expect(result.current.isDirty).toBe(false);
    act(() => {
      result.current.setField("name", "Typed");
    });
    expect(result.current.changes).toEqual({ name: "Typed" });
    expect(result.current.isDirty).toBe(true);
    act(() => {
      result.current.conflict({ name: "Theirs", questions: ["a"], on: true }, "AAAAAAAA002=");
    });
    expect(result.current.conflicts).toEqual({ name: "Theirs", on: true });
    act(() => {
      result.current.setField("on", true);
    });
    expect(result.current.conflicts).toEqual({ name: "Theirs" });
    act(() => {
      result.current.takeLatest("name");
      result.current.moveRowVersion("AAAAAAAA002=", "AAAAAAAA003=");
    });
    expect(result.current.draft).toEqual({ name: "Theirs", questions: ["a"], on: true });
    expect(result.current.rowVersion).toBe("AAAAAAAA003=");
    act(() => {
      result.current.replaceDraft({ ...BASE });
      result.current.saved(BASE, "AAAAAAAA004=");
      result.current.reload({ ...BASE, name: "Later" }, "AAAAAAAA005=");
    });
    expect(result.current.base.name).toBe("Later");
  });
});
