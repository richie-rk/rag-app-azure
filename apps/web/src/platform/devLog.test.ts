import { describe, expect, test, vi } from "vitest";

import { devLog, devLogOutcome } from "./devLog";

describe("devLog", () => {
  test("in development it writes MSAL errors without PII, and outcome codes with reference IDs", () => {
    const error = vi.spyOn(console, "error").mockImplementation(() => undefined);
    const info = vi.spyOn(console, "info").mockImplementation(() => undefined);
    devLog(0, "token request failed", false);
    devLog(0, "user ada@example.test", true);
    devLog(1, "a warning", false);
    devLogOutcome("stream_stalled", "abc");
    expect(error).toHaveBeenCalledExactlyOnceWith("[msal]", "token request failed");
    expect(info).toHaveBeenCalledExactlyOnceWith({ code: "stream_stalled", referenceId: "abc" });
  });

  test("in production builds it writes nothing", () => {
    vi.stubEnv("DEV", false);
    const error = vi.spyOn(console, "error").mockImplementation(() => undefined);
    const info = vi.spyOn(console, "info").mockImplementation(() => undefined);
    devLog(0, "token request failed", false);
    devLogOutcome("stream_stalled", null);
    expect(error).not.toHaveBeenCalled();
    expect(info).not.toHaveBeenCalled();
  });
});
