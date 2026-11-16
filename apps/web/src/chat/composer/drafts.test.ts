import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, test } from "vitest";

import {
  draftKey,
  getDraft,
  resetDraftsForTests,
  setDraft,
  setDraftReason,
  useDraft,
} from "./drafts";

afterEach(() => {
  act(() => {
    resetDraftsForTests();
  });
});

describe("composer drafts", () => {
  test("are keyed by session, or by new chat and project", () => {
    expect(draftKey("s", "p")).toBe("s");
    expect(draftKey(null, "p")).toBe("new:p");
    expect(draftKey(null, null)).toBe("new:");
  });

  test("are kept per key in memory, and a reason shows until the text changes", () => {
    const { result } = renderHook(({ key }) => useDraft(key), { initialProps: { key: "a" } });
    act(() => {
      result.current.setText("Hello");
    });
    expect(result.current.text).toBe("Hello");
    expect(getDraft("b")).toBe("");
    act(() => {
      setDraft("a", "Back");
      setDraftReason("a", "Too long");
    });
    expect(result.current).toMatchObject({ text: "Back", reason: "Too long" });
    act(() => {
      result.current.setText("Back again");
    });
    expect(result.current.reason).toBeNull();
    act(() => {
      setDraft("a", "");
      setDraftReason("a", null);
    });
    expect(result.current).toMatchObject({ text: "", reason: null });
  });
});
