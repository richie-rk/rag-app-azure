// A stand-in for MSAL's PublicClientApplication: every method the client and MsalProvider call,
// as Vitest mocks the tests script. The browser suites run the real MSAL against a stub Entra ID.

import type {
  AccountInfo,
  AuthenticationResult,
  IPublicClientApplication,
} from "@azure/msal-browser";
import { vi } from "vitest";

import { TEST_CONFIG } from "./handlers";

export const TEST_ACCOUNT: AccountInfo = {
  homeAccountId: "home-account",
  environment: "login.microsoftonline.com",
  tenantId: TEST_CONFIG.tenantId,
  username: "ada@example.test",
  localAccountId: "local-account",
  name: "Ada Lovelace",
};

const logger = {
  clone: () => logger,
  error: () => undefined,
  warning: () => undefined,
  info: () => undefined,
  verbose: () => undefined,
  trace: () => undefined,
};

export function fakeMsal(accounts: AccountInfo[] = [TEST_ACCOUNT]) {
  const instance = {
    initialize: vi.fn(() => Promise.resolve()),
    initializeWrapperLibrary: vi.fn(),
    getLogger: vi.fn(() => logger),
    addEventCallback: vi.fn(() => "callback"),
    removeEventCallback: vi.fn(),
    handleRedirectPromise: vi.fn((): Promise<AuthenticationResult | null> => Promise.resolve(null)),
    getAllAccounts: vi.fn(() => accounts),
    getActiveAccount: vi.fn((): AccountInfo | null => accounts[0] ?? null),
    setActiveAccount: vi.fn(),
    loginRedirect: vi.fn(() => Promise.resolve()),
    acquireTokenRedirect: vi.fn(() => Promise.resolve()),
    acquireTokenSilent: vi.fn(() =>
      Promise.resolve({ accessToken: "access-token" } as AuthenticationResult),
    ),
    logoutRedirect: vi.fn(() => Promise.resolve()),
    clearCache: vi.fn(() => Promise.resolve()),
  };
  return { instance, msal: instance as unknown as IPublicClientApplication };
}

export type FakeMsal = ReturnType<typeof fakeMsal>["instance"];
