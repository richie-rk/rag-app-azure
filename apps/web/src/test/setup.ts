// Vitest setup: jest-dom matchers, jitless Zod as in main.tsx, the MSW server failing any
// unhandled request, the browser APIs jsdom lacks, and a clean slate after each test.

import "@testing-library/jest-dom/vitest";
import "../bootstrap/zodSetup";

import { cleanup, configure } from "@testing-library/react";
import { afterAll, afterEach, beforeAll, beforeEach } from "vitest";

import { resetHttpForTests } from "../api/http";
import { resetThemeForTests } from "../shell/theme";
import { installMatchMedia } from "./media";
import { server } from "./server";

class NoopResizeObserver {
  observe(): void {
    // jsdom lays nothing out, so nothing ever resizes.
  }
  unobserve(): void {
    // As above.
  }
  disconnect(): void {
    // As above.
  }
}

// Fluent's popovers and dialogs take longer than Testing Library's 1 s default to appear while
// the test files run in parallel.
configure({ asyncUtilTimeout: 5_000 });

beforeAll(() => {
  // MSW 3's name for MSW 2's `onUnhandledRequest`: any request without a handler fails the test.
  server.listen({ onUnhandledFrame: "error" });
});

// The script and configuration tests run in Node's own environment, which has no DOM.
const browser = typeof window !== "undefined";

beforeEach(() => {
  if (!browser) return;
  installMatchMedia();
  if (!("ResizeObserver" in window)) {
    Object.defineProperty(window, "ResizeObserver", {
      configurable: true,
      writable: true,
      value: NoopResizeObserver,
    });
  }
});

afterEach(() => {
  cleanup();
  server.resetHandlers();
  resetHttpForTests();
  resetThemeForTests();
  if (!browser) return;
  window.sessionStorage.clear();
  window.localStorage.clear();
  document.title = "";
});

afterAll(() => {
  server.close();
});
