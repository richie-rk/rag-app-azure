import { act, renderHook } from "@testing-library/react";
import { describe, expect, test } from "vitest";

import { setViewportWidth } from "../test/media";
import { WIDE_QUERY, useBreakpoint, useMediaQuery } from "./media";

describe("breakpoints", () => {
  test("the layout follows the viewport across 768 px and 1,280 px", () => {
    const { result } = renderHook(() => useBreakpoint());
    expect(result.current).toBe("wide");
    act(() => {
      setViewportWidth(1_279);
    });
    expect(result.current).toBe("medium");
    act(() => {
      setViewportWidth(767);
    });
    expect(result.current).toBe("narrow");
    act(() => {
      setViewportWidth(1_280);
    });
    expect(result.current).toBe("wide");
  });

  test("without matchMedia nothing matches", () => {
    // The setup file installs the fake again before the next test.
    Object.defineProperty(window, "matchMedia", {
      configurable: true,
      writable: true,
      value: undefined,
    });
    const { result, unmount } = renderHook(() => useMediaQuery(WIDE_QUERY));
    expect(result.current).toBe(false);
    unmount();
  });
});
