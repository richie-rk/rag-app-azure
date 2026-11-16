import {
  AuthError,
  InteractionRequiredAuthError,
  LogLevel,
  createStandardPublicClientApplication,
} from "@azure/msal-browser";
import { describe, expect, test, vi } from "vitest";

import { devLog } from "../platform/devLog";
import { TEST_ACCOUNT, fakeMsal } from "../test/fakeMsal";
import { TEST_CONFIG } from "../test/handlers";
import {
  classifyRedirectError,
  createMsalInstance,
  msalConfiguration,
  pickAccount,
  safeReturnPath,
} from "./msal";

vi.mock("@azure/msal-browser", async (original) => ({
  ...(await original<typeof import("@azure/msal-browser")>()),
  createStandardPublicClientApplication: vi.fn(() => Promise.resolve({ instance: true })),
}));

const ORIGIN = "https://docchat.example";

describe("the MSAL configuration", () => {
  test("is exactly section 4.1's", () => {
    expect(msalConfiguration(TEST_CONFIG, ORIGIN)).toEqual({
      auth: {
        clientId: TEST_CONFIG.clientId,
        authority: `https://login.microsoftonline.com/${TEST_CONFIG.tenantId}`,
        redirectUri: `${ORIGIN}/`,
        postLogoutRedirectUri: `${ORIGIN}/`,
      },
      cache: { cacheLocation: "sessionStorage" },
      system: {
        allowRedirectInIframe: false,
        loggerOptions: {
          logLevel: LogLevel.Error,
          piiLoggingEnabled: false,
          loggerCallback: devLog,
        },
      },
    });
  });

  test("one standard instance is created from it", async () => {
    await expect(createMsalInstance(TEST_CONFIG, ORIGIN)).resolves.toEqual({ instance: true });
    expect(createStandardPublicClientApplication).toHaveBeenCalledWith(
      msalConfiguration(TEST_CONFIG, ORIGIN),
    );
  });
});

describe("the return path", () => {
  test.each([
    ["/memory", "/memory"],
    ["/chat/abc?project=1", "/chat/abc?project=1"],
    [`/${"a".repeat(511)}`, `/${"a".repeat(511)}`],
  ])("T-349 %s is kept", (state, expected) => {
    expect(safeReturnPath(state, ORIGIN)).toBe(expected);
  });

  test.each([
    ["//evil.example"],
    ["https://evil.example/"],
    ["/\\evil.example"],
    ["\\\\evil.example"],
    ["/\t/evil.example"],
    ["memory"],
    [""],
    [`/${"a".repeat(512)}`],
    [null],
    [undefined],
  ])("T-349 a tampered state %j lands on /chat", (state) => {
    expect(safeReturnPath(state, ORIGIN)).toBe("/chat");
  });

  test("a state that the URL parser refuses lands on /chat", () => {
    expect(safeReturnPath("/memory", "not an origin")).toBe("/chat");
  });
});

describe("the account", () => {
  test("the redirect result's account comes first, else this tenant's first cached account", () => {
    const other = {
      ...TEST_ACCOUNT,
      homeAccountId: "other",
      tenantId: "99999999-9999-4999-8999-999999999999",
    };
    const { msal } = fakeMsal([other, TEST_ACCOUNT]);
    const fromResult = { ...TEST_ACCOUNT, homeAccountId: "result" };
    expect(pickAccount(msal, TEST_CONFIG.tenantId, fromResult)).toBe(fromResult);
    expect(pickAccount(msal, TEST_CONFIG.tenantId, null)).toBe(TEST_ACCOUNT);
    expect(pickAccount(fakeMsal([other]).msal, TEST_CONFIG.tenantId, null)).toBeNull();
  });
});

describe("sign-in errors", () => {
  test("T-350 AADSTS50105 shows Not assigned", () => {
    expect(
      classifyRedirectError(new AuthError("access_denied", "c1", "AADSTS50105: not assigned")),
    ).toEqual({
      state: "not_assigned",
    });
    expect(
      classifyRedirectError(
        new InteractionRequiredAuthError(
          "interaction_required",
          "c1",
          "x",
          "",
          "",
          "",
          "",
          "50105",
        ),
      ),
    ).toEqual({ state: "not_assigned" });
  });

  test("T-350 access_denied alone and a cancelled sign-in show Signed out", () => {
    expect(classifyRedirectError(new AuthError("access_denied", "c1", "denied"))).toEqual({
      state: "signed_out",
    });
    expect(classifyRedirectError(new AuthError("user_cancelled", "c1"))).toEqual({
      state: "signed_out",
    });
  });

  test("T-350 AADSTS65001 and AADSTS90094 show Consent missing with the reference ID", () => {
    expect(
      classifyRedirectError(new AuthError("invalid_grant", "c2", "AADSTS65001: consent")),
    ).toEqual({
      state: "consent_missing",
      referenceId: "c2",
    });
    expect(
      classifyRedirectError(new AuthError("invalid_grant", "c3", "AADSTS90094: admin")),
    ).toEqual({
      state: "consent_missing",
      referenceId: "c3",
    });
  });

  test("T-350 any other error shows Sign-in failed with the reference ID", () => {
    expect(classifyRedirectError(new AuthError("server_error", "c4", "AADSTS50011"))).toEqual({
      state: "signin_failed",
      referenceId: "c4",
    });
    expect(classifyRedirectError(new AuthError("server_error", ""))).toEqual({
      state: "signin_failed",
      referenceId: null,
    });
    expect(classifyRedirectError(new Error("not MSAL"))).toEqual({
      state: "signin_failed",
      referenceId: null,
    });
  });
});
