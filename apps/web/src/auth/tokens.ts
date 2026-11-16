// The only caller of MSAL's token methods (10-web-client.md sections 4.4 and 4.5). `http.ts` asks
// here for a token before every API call. One module-level guard makes concurrent expiries share
// one redirect and one session-expired state.

import {
  CacheLookupPolicy,
  InteractionRequiredAuthError,
  type AccountInfo,
  type IPublicClientApplication,
} from "@azure/msal-browser";
import { useSyncExternalStore } from "react";

import { ApiFailure, clientError } from "../api/problem";

export interface TokenSetup {
  readonly msal: IPublicClientApplication;
  readonly account: AccountInfo;
  /** `config.apiScope`: every request names exactly this one scope. */
  readonly scope: string;
}

let setup: TokenSetup | null = null;
let pendingSilent: Promise<string> | null = null;
let pendingRenewal: Promise<string> | null = null;
let shellRendered = false;
let sessionExpired = false;
let redirecting = false;
const listeners = new Set<() => void>();

/** Called once at start-up, with the account start-up picked (section 4.2). */
export function initTokens(value: TokenSetup): void {
  setup = value;
  pendingSilent = null;
  pendingRenewal = null;
  shellRendered = false;
  sessionExpired = false;
  redirecting = false;
}

function requireSetup(): TokenSetup {
  if (setup === null) throw new Error("initTokens hasn't run");
  return setup;
}

/** Once the shell is on screen the client never redirects on its own, so the user can copy unsent
 * text before signing in again (section 4.5 step 5). */
export function markShellRendered(): void {
  shellRendered = true;
}

export function isSessionExpired(): boolean {
  return sessionExpired;
}

/** Sets the session-expired state: the Session expired bar shows and every new call fails at once
 * with `session_expired`; streams and uploads already under way continue. */
export function expireSession(): void {
  if (sessionExpired) return;
  sessionExpired = true;
  for (const listener of listeners) listener();
}

export function subscribeSessionExpired(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** Whether the session has expired, as React state. */
export function useSessionExpired(): boolean {
  return useSyncExternalStore(subscribeSessionExpired, isSessionExpired);
}

/** The in-app path an interactive request returns to (section 4.3). */
export function currentReturnPath(): string {
  return window.location.pathname + window.location.search;
}

function sessionExpiredFailure(): ApiFailure {
  return new ApiFailure(clientError("session_expired", { referenceId: null }));
}

/** Never settles: the page is navigating away to Entra ID. */
function never(): Promise<string> {
  return new Promise<string>(() => undefined);
}

/** Starts `acquireTokenRedirect` once, however many requests need it (section 4.4 step 4). */
function redirectForInteraction(): Promise<string> {
  const { msal, account, scope } = requireSetup();
  if (!redirecting) {
    redirecting = true;
    void msal
      .acquireTokenRedirect({ scopes: [scope], account, state: currentReturnPath() })
      .catch(() => {
        // A redirect that can't start leaves the Starting screen up; there is nothing to recover.
        redirecting = false;
      });
  }
  return never();
}

async function acquire(cacheLookupPolicy: CacheLookupPolicy): Promise<string> {
  const { msal, account, scope } = requireSetup();
  try {
    const result = await msal.acquireTokenSilent({ scopes: [scope], account, cacheLookupPolicy });
    return result.accessToken;
  } catch (error) {
    if (error instanceof InteractionRequiredAuthError) {
      if (!shellRendered) return redirectForInteraction();
      expireSession();
      throw sessionExpiredFailure();
    }
    // Entra ID unreachable, for one: no redirect starts.
    const correlationId =
      typeof error === "object" && error !== null && "correlationId" in error
        ? String(error.correlationId) || null
        : null;
    throw new ApiFailure(clientError("network", { referenceId: correlationId }));
  }
}

/** The access token for the next API call: the cached one, or one redeemed from the refresh token
 * over `fetch`; never through an iframe. A pending silent request is shared. */
export function getAccessToken(): Promise<string> {
  if (sessionExpired) return Promise.reject(sessionExpiredFailure());
  pendingSilent ??= acquire(CacheLookupPolicy.AccessTokenAndRefreshToken).finally(() => {
    pendingSilent = null;
  });
  return pendingSilent;
}

/** A token redeemed from the refresh token, skipping the cached access token, after the API
 * answered `401` (section 4.5 step 2). */
export function renewAccessToken(): Promise<string> {
  if (sessionExpired) return Promise.reject(sessionExpiredFailure());
  pendingRenewal ??= acquire(CacheLookupPolicy.RefreshToken).finally(() => {
    pendingRenewal = null;
  });
  return pendingRenewal;
}

/** The Session expired bar's Sign in: a top-level visit to Entra ID that returns to this screen. */
export function signInAgain(): Promise<void> {
  const { msal, account, scope } = requireSetup();
  return msal.acquireTokenRedirect({ scopes: [scope], account, state: currentReturnPath() });
}
