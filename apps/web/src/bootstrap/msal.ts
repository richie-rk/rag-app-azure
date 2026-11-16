// MSAL: the one instance per page load, the account start-up picks, the safe return path and the
// access state each sign-in error causes (10-web-client.md sections 4.1, 4.3 and 4.8).

import {
  AuthError,
  BrowserCacheLocation,
  LogLevel,
  createStandardPublicClientApplication,
  type AccountInfo,
  type Configuration,
  type IPublicClientApplication,
} from "@azure/msal-browser";

import { RETURN_PATH_MAX } from "../platform/constants";
import { devLog } from "../platform/devLog";
import type { AppConfig } from "./loadConfig";

/** Exactly section 4.1's configuration: the tenant's own authority, the redirect URI following the
 * page's origin, the cache in session storage, redirects only, and errors logged in development
 * without PII. */
export function msalConfiguration(config: AppConfig, origin: string): Configuration {
  return {
    auth: {
      clientId: config.clientId,
      authority: `https://login.microsoftonline.com/${config.tenantId}`,
      redirectUri: `${origin}/`,
      postLogoutRedirectUri: `${origin}/`,
    },
    cache: { cacheLocation: BrowserCacheLocation.SessionStorage },
    system: {
      allowRedirectInIframe: false,
      loggerOptions: { logLevel: LogLevel.Error, piiLoggingEnabled: false, loggerCallback: devLog },
    },
  };
}

/** Creates and initialises the instance (section 4.2 step 4). */
export function createMsalInstance(
  config: AppConfig,
  origin: string = window.location.origin,
): Promise<IPublicClientApplication> {
  return createStandardPublicClientApplication(msalConfiguration(config, origin));
}

/** The return path of an interactive request (section 4.3): `state` only when it starts with
 * exactly one `/`, holds no `\`, is at most 512 characters long and stays on this origin;
 * otherwise `/chat`. The rule closes an open-redirect path through `state`. */
export function safeReturnPath(state: string | null | undefined, origin: string): string {
  if (
    typeof state !== "string" ||
    !state.startsWith("/") ||
    state.startsWith("//") ||
    state.includes("\\") ||
    state.length > RETURN_PATH_MAX
  ) {
    return "/chat";
  }
  try {
    return new URL(state, origin).origin === origin ? state : "/chat";
  } catch {
    return "/chat";
  }
}

/** The redirect result's account, else the first cached account of this tenant; accounts of other
 * tenants are ignored (section 4.2 step 6). */
export function pickAccount(
  msal: IPublicClientApplication,
  tenantId: string,
  resultAccount: AccountInfo | null,
): AccountInfo | null {
  if (resultAccount !== null) return resultAccount;
  return msal.getAllAccounts().find((account) => account.tenantId === tenantId) ?? null;
}

export type RedirectErrorState =
  | { readonly state: "signed_out" }
  | { readonly state: "not_assigned" }
  | { readonly state: "consent_missing"; readonly referenceId: string | null }
  | { readonly state: "signin_failed"; readonly referenceId: string | null };

function mentions(error: AuthError, code: string): boolean {
  return error.errorMessage.includes(`AADSTS${code}`);
}

/** The access state that an error from `handleRedirectPromise` causes (section 4.8). */
export function classifyRedirectError(error: unknown): RedirectErrorState {
  if (!(error instanceof AuthError)) return { state: "signin_failed", referenceId: null };
  const referenceId = error.correlationId === "" ? null : error.correlationId;
  const errorNo = (error as { errorNo?: unknown }).errorNo;
  if (errorNo === "50105" || mentions(error, "50105")) return { state: "not_assigned" };
  if (error.errorCode === "user_cancelled" || error.errorCode === "access_denied") {
    return { state: "signed_out" };
  }
  if (mentions(error, "65001") || mentions(error, "90094")) {
    return { state: "consent_missing", referenceId };
  }
  return { state: "signin_failed", referenceId };
}
