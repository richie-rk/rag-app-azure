import { webDarkTheme, webLightTheme } from "@fluentui/react-components";
import { act, renderHook } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";

import { setSystemDark } from "../test/media";
import { currentTheme, fluentTheme, setTheme, subscribeTheme, useThemeChoice } from "./theme";

describe("the theme", () => {
  test("follows prefers-color-scheme until a choice is remembered", () => {
    expect(currentTheme()).toBe("light");
    const { result } = renderHook(() => useThemeChoice());
    act(() => {
      setSystemDark(true);
    });
    expect(result.current).toBe("dark");
    act(() => {
      setTheme("light");
    });
    expect(result.current).toBe("light");
    expect(window.localStorage.getItem("docchat.theme")).toBe("light");
    act(() => {
      setSystemDark(false);
      setSystemDark(true);
    });
    expect(result.current).toBe("light");
  });

  test("a remembered choice applies at start", () => {
    window.localStorage.setItem("docchat.theme", "dark");
    expect(currentTheme()).toBe("dark");
  });

  test("a system change doesn't notify once a choice is remembered", () => {
    window.localStorage.setItem("docchat.theme", "dark");
    const listener = vi.fn();
    const unsubscribe = subscribeTheme(listener);
    setSystemDark(true);
    expect(listener).not.toHaveBeenCalled();
    unsubscribe();
  });

  test("without matchMedia the theme is light", () => {
    // The setup file installs the fake again before the next test.
    Object.defineProperty(window, "matchMedia", {
      configurable: true,
      writable: true,
      value: undefined,
    });
    expect(currentTheme()).toBe("light");
    const unsubscribe = subscribeTheme(() => undefined);
    unsubscribe();
  });

  test("the themes are Fluent's, unmodified", () => {
    expect(fluentTheme("light")).toBe(webLightTheme);
    expect(fluentTheme("dark")).toBe(webDarkTheme);
  });
});
