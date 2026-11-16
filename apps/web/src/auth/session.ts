// Signing out, in this tab and the others (10-web-client.md section 4.6), and the signed-in
// session the shell reads: the account and the sign-out action.

import type { AccountInfo, IPublicClientApplication } from "@azure/msal-browser";
import type { QueryClient } from "@tanstack/react-query";
import { createContext, useContext } from "react";

import { haltApi } from "../api/http";
import { abortAllWork } from "../platform/activeWork";
import type { AuthChannel } from "../platform/authChannel";
import { setSignedOut } from "../platform/storage";

export interface SignOutDependencies {
  readonly msal: IPublicClientApplication;
  /** Null when no account was picked, such as after a sign-in error. */
  readonly account: AccountInfo | null;
  readonly channel: AuthChannel | null;
  readonly queryClient: QueryClient | null;
  /** Shows the Signed out screen when `logoutRedirect` can't start. */
  readonly showSignedOut: () => void;
}

/** Signs out of this tab: steps 2 to 5 of section 4.6. The caller has already asked Sign out
 * now? when work was running. */
export async function signOut(deps: SignOutDependencies): Promise<void> {
  setSignedOut();
  deps.channel?.postSignedOut();
  abortAllWork();
  haltApi();
  deps.queryClient?.clear();
  try {
    await deps.msal.logoutRedirect({
      ...(deps.account === null ? {} : { account: deps.account }),
      postLogoutRedirectUri: `${window.location.origin}/`,
    });
  } catch {
    await deps.msal.clearCache().catch(() => undefined);
    deps.showSignedOut();
  }
}

/** Another tab signed out (section 4.6 step 6): this tab's tokens go too, because session storage
 * is per tab. */
export async function signedOutElsewhere(deps: SignOutDependencies): Promise<void> {
  abortAllWork();
  haltApi();
  deps.queryClient?.clear();
  await deps.msal.clearCache().catch(() => undefined);
  setSignedOut();
  deps.showSignedOut();
}

export interface AppSession {
  readonly account: AccountInfo;
  /** Signs out (section 4.6); the shell asks Sign out now? first when work is running. */
  readonly signOut: () => Promise<void>;
}

export const SessionContext = createContext<AppSession | null>(null);

/** The signed-in session; only inside the app shell. */
export function useSession(): AppSession {
  const session = useContext(SessionContext);
  if (session === null) throw new Error("useSession outside the signed-in app");
  return session;
}
