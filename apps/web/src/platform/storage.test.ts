import { describe, expect, test, vi } from "vitest";

import {
  clearSignedOut,
  readNavOpen,
  readSignedOut,
  readTheme,
  setSignedOut,
  writeNavOpen,
  writeTheme,
} from "./storage";

describe("browser storage", () => {
  test("the signed-out flag lives in session storage as 1", () => {
    expect(readSignedOut()).toBe(false);
    setSignedOut();
    expect(window.sessionStorage.getItem("docchat.signedOut")).toBe("1");
    expect(readSignedOut()).toBe(true);
    clearSignedOut();
    expect(readSignedOut()).toBe(false);
    window.sessionStorage.setItem("docchat.signedOut", "yes");
    expect(readSignedOut()).toBe(false);
  });

  test("the theme lives in local storage as light or dark", () => {
    expect(readTheme()).toBeNull();
    writeTheme("dark");
    expect(window.localStorage.getItem("docchat.theme")).toBe("dark");
    expect(readTheme()).toBe("dark");
    writeTheme("light");
    expect(readTheme()).toBe("light");
    window.localStorage.setItem("docchat.theme", "sepia");
    expect(readTheme()).toBeNull();
  });

  test("the navigation preference lives in local storage as true or false", () => {
    expect(readNavOpen()).toBeNull();
    writeNavOpen(false);
    expect(window.localStorage.getItem("docchat.nav.open")).toBe("false");
    expect(readNavOpen()).toBe(false);
    writeNavOpen(true);
    expect(readNavOpen()).toBe(true);
    window.localStorage.setItem("docchat.nav.open", "1");
    expect(readNavOpen()).toBeNull();
  });

  test("a failing storage means the default and never throws", () => {
    const fail = () => {
      throw new DOMException("denied", "SecurityError");
    };
    const proto = Object.getPrototypeOf(window.localStorage) as Storage;
    vi.spyOn(proto, "getItem").mockImplementation(fail);
    vi.spyOn(proto, "setItem").mockImplementation(fail);
    vi.spyOn(proto, "removeItem").mockImplementation(fail);
    expect(readSignedOut()).toBe(false);
    expect(readTheme()).toBeNull();
    expect(readNavOpen()).toBeNull();
    expect(() => {
      setSignedOut();
      clearSignedOut();
      writeTheme("dark");
      writeNavOpen(true);
    }).not.toThrow();
  });
});
