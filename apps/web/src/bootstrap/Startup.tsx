// Start-up (10-web-client.md section 4.2): each step either moves on or ends in a screen of
// section 4.9, so no state waits without end. The page renders into one root: first the access
// screens, then the providers and the router once a signed-in account is picked; a later access
// state (Not assigned, Signed out) replaces the whole tree.

import type {
  AccountInfo,
  AuthenticationResult,
  IPublicClientApplication,
} from "@azure/msal-browser";
import { MsalProvider } from "@azure/msal-react";
import { FluentProvider, makeStyles, tokens } from "@fluentui/react-components";
import { QueryClientProvider, type QueryClient } from "@tanstack/react-query";
import { useEffect, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { RouterProvider } from "react-router/dom";

import { configureHttp, haltApi, startChatTurn } from "../api/http";
import { createQueryClient, queryKeys } from "../api/queryClient";
import { LiveRegions } from "../a11y/LiveRegions";
import {
  ConfigErrorScreen,
  ConsentMissingScreen,
  NotAssignedScreen,
  RedirectingScreen,
  SignInFailedScreen,
  SignedOutScreen,
  StartingScreen,
  UnsupportedBrowserScreen,
} from "../auth/AccessScreens";
import { SessionContext, signOut, signedOutElsewhere, type AppSession } from "../auth/session";
import { expireSession, getAccessToken, initTokens, renewAccessToken } from "../auth/tokens";
import { createTurnSpeech } from "../chat/render/speakable";
import { TurnStore, TurnStoreContext } from "../chat/stream/turnStore";
import { installUnloadGuard } from "../platform/activeWork";
import { openAuthChannel, type AuthChannel } from "../platform/authChannel";
import { clearSignedOut, readSignedOut } from "../platform/storage";
import { createAppRouter, type AppRouter } from "../shell/routes";
import { fluentTheme, useThemeChoice } from "../shell/theme";
import { ConfigContext, loadConfig, type AppConfig } from "./loadConfig";
import { classifyRedirectError, createMsalInstance, pickAccount, safeReturnPath } from "./msal";

/** What start-up reads from the browser; tests replace it. */
export interface StartupEnvironment {
  readonly hasRequiredApis: () => boolean;
  readonly loadConfig: () => Promise<AppConfig>;
  readonly createMsal: (config: AppConfig) => Promise<IPublicClientApplication>;
  readonly location: {
    readonly origin: string;
    readonly pathname: string;
    readonly search: string;
  };
  /** Replaces the location before the router exists. */
  readonly replacePath: (path: string) => void;
  readonly reload: () => void;
  readonly openChannel: (onSignedOut: () => void) => AuthChannel;
  readonly createRouter: () => AppRouter;
}

/** The browser APIs the client needs before any network call (section 1.9). */
export function hasRequiredBrowserApis(scope: Partial<typeof globalThis> = globalThis): boolean {
  return (
    typeof scope.ReadableStream === "function" &&
    typeof scope.TextDecoder === "function" &&
    typeof scope.crypto?.subtle === "object" &&
    typeof scope.crypto.randomUUID === "function" &&
    typeof scope.BroadcastChannel === "function" &&
    typeof scope.AbortController === "function"
  );
}

export function browserEnvironment(): StartupEnvironment {
  return {
    hasRequiredApis: () => hasRequiredBrowserApis(),
    loadConfig: () => loadConfig(window.location.hostname),
    createMsal: (config) => createMsalInstance(config, window.location.origin),
    location: window.location,
    replacePath: (path) => {
      window.history.replaceState(null, "", path);
    },
    reload: () => {
      window.location.reload();
    },
    openChannel: openAuthChannel,
    createRouter: createAppRouter,
  };
}

export type StartupOutcome =
  | { readonly kind: "config_error" }
  | {
      readonly kind: "signin_failed";
      readonly referenceId: string | null;
      readonly msal: IPublicClientApplication | null;
      readonly config: AppConfig;
    }
  | {
      readonly kind: "not_assigned";
      readonly msal: IPublicClientApplication;
      readonly config: AppConfig;
    }
  | {
      readonly kind: "consent_missing";
      readonly referenceId: string | null;
      readonly msal: IPublicClientApplication;
      readonly config: AppConfig;
    }
  | {
      readonly kind: "signed_out";
      readonly msal: IPublicClientApplication;
      readonly config: AppConfig;
    }
  | {
      readonly kind: "sign_in_required";
      readonly msal: IPublicClientApplication;
      readonly config: AppConfig;
    }
  | {
      readonly kind: "ready";
      readonly msal: IPublicClientApplication;
      readonly config: AppConfig;
      readonly account: AccountInfo;
    };

/** Steps 3 to 6: the configuration, the MSAL instance, the redirect result and the account. */
export async function runStartup(env: StartupEnvironment): Promise<StartupOutcome> {
  let config: AppConfig;
  try {
    config = await env.loadConfig();
  } catch {
    return { kind: "config_error" };
  }
  let msal: IPublicClientApplication;
  try {
    msal = await env.createMsal(config);
  } catch {
    return { kind: "signin_failed", referenceId: null, msal: null, config };
  }
  let result: AuthenticationResult | null;
  try {
    result = await msal.handleRedirectPromise({ navigateToLoginRequestUrl: false });
  } catch (error) {
    const state = classifyRedirectError(error);
    switch (state.state) {
      case "signed_out":
        return { kind: "signed_out", msal, config };
      case "not_assigned":
        return { kind: "not_assigned", msal, config };
      case "consent_missing":
        return { kind: "consent_missing", referenceId: state.referenceId, msal, config };
      case "signin_failed":
        return { kind: "signin_failed", referenceId: state.referenceId, msal, config };
    }
  }
  if (result !== null) {
    clearSignedOut();
    env.replacePath(safeReturnPath(result.state, env.location.origin));
  }
  const account = pickAccount(msal, config.tenantId, result?.account ?? null);
  if (account === null) {
    return readSignedOut()
      ? { kind: "signed_out", msal, config }
      : { kind: "sign_in_required", msal, config };
  }
  msal.setActiveAccount(account);
  return { kind: "ready", msal, config, account };
}

const useFrameStyles = makeStyles({
  // On a box inside the provider, not on the provider: Fluent gives every portal's mount node the
  // provider's class names, and a mount node at least 100vh high, above everything, would cover
  // the page and take the pointer from it whenever a listbox, menu or tooltip is mounted.
  page: { minHeight: "100vh", backgroundColor: tokens.colorNeutralBackground1 },
  body: { margin: "0", backgroundColor: tokens.colorNeutralBackground1 },
});

/** The Fluent theme and the live regions around every state, so the regions are present and empty
 * from the first paint and stay in place when the page's content changes. */
function Frame({ children }: { readonly children: ReactNode }) {
  const styles = useFrameStyles();
  const theme = useThemeChoice();
  useEffect(() => {
    // Griffel gives several class names in one string.
    const classes = styles.body.split(" ").filter(Boolean);
    document.body.classList.add(...classes);
    return () => {
      document.body.classList.remove(...classes);
    };
  }, [styles.body]);
  return (
    <FluentProvider theme={fluentTheme(theme)}>
      <div className={styles.page}>{children}</div>
      <LiveRegions />
    </FluentProvider>
  );
}

export interface AppProvidersProps {
  readonly msal: IPublicClientApplication;
  readonly queryClient: QueryClient;
  readonly config: AppConfig;
  readonly session: AppSession;
  readonly turnStore: TurnStore;
  readonly router: AppRouter;
}

/** Step 8's providers around the router. */
export function AppProviders({
  msal,
  queryClient,
  config,
  session,
  turnStore,
  router,
}: AppProvidersProps) {
  return (
    <MsalProvider instance={msal}>
      <QueryClientProvider client={queryClient}>
        <ConfigContext.Provider value={config}>
          <SessionContext.Provider value={session}>
            <TurnStoreContext.Provider value={turnStore}>
              <RouterProvider router={router} />
            </TurnStoreContext.Provider>
          </SessionContext.Provider>
        </ConfigContext.Provider>
      </QueryClientProvider>
    </MsalProvider>
  );
}

class Page {
  constructor(private readonly root: Root) {}

  show(node: ReactNode): void {
    this.root.render(<Frame>{node}</Frame>);
  }
}

function loginRedirect(
  msal: IPublicClientApplication,
  config: AppConfig,
  env: StartupEnvironment,
  page: Page,
): void {
  msal
    .loginRedirect({
      scopes: [config.apiScope],
      state: env.location.pathname + env.location.search,
    })
    .catch((error: unknown) => {
      const state = classifyRedirectError(error);
      page.show(
        <SignInFailedScreen
          referenceId={"referenceId" in state ? state.referenceId : null}
          onRetry={() => {
            loginRedirect(msal, config, env, page);
          }}
        />,
      );
    });
}

function showSignedOut(
  msal: IPublicClientApplication,
  config: AppConfig,
  env: StartupEnvironment,
  page: Page,
): void {
  page.show(
    <SignedOutScreen
      onSignIn={() => {
        clearSignedOut();
        loginRedirect(msal, config, env, page);
      }}
    />,
  );
}

/** Not assigned (section 4.9): Retry signs in once more and lets the user pick another account. */
function showNotAssigned(
  msal: IPublicClientApplication,
  config: AppConfig,
  account: AccountInfo | null,
  onSignOut: () => void,
  page: Page,
): void {
  page.show(
    <NotAssignedScreen
      account={account?.username ?? ""}
      onRetry={() => {
        void msal
          .loginRedirect({ scopes: [config.apiScope], prompt: "select_account" })
          .catch(() => undefined);
      }}
      onSignOut={onSignOut}
    />,
  );
}

/** Steps 7 to 9 for a signed-in account: the sign-out channel, the token and API modules, the
 * query client and the router, rendered inside the providers. */
function startApp(
  ready: Extract<StartupOutcome, { kind: "ready" }>,
  env: StartupEnvironment,
  page: Page,
): void {
  const { msal, config, account } = ready;
  initTokens({ msal, account, scope: config.apiScope });
  const queryClient = createQueryClient();
  let channel: AuthChannel | null = null;
  const signOutDependencies = () => ({
    msal,
    account,
    channel,
    queryClient,
    showSignedOut: () => {
      showSignedOut(msal, config, env, page);
    },
  });
  const notAssigned = () => {
    // Every query stops and the shell is replaced; no other API call follows.
    haltApi();
    void queryClient.cancelQueries();
    queryClient.clear();
    showNotAssigned(msal, config, account, () => void signOut(signOutDependencies()), page);
  };
  configureHttp({
    apiBaseUrl: config.apiBaseUrl,
    getToken: getAccessToken,
    renewToken: renewAccessToken,
    onSessionExpired: expireSession,
    onRoleNotAssigned: notAssigned,
    // The role was removed during the session: refetch the user, and the admin guard shows Admin
    // only once `isAdmin` is false.
    onAdminRequired: () => void queryClient.invalidateQueries({ queryKey: queryKeys.me }),
  });
  channel = env.openChannel(() => void signedOutElsewhere(signOutDependencies()));
  installUnloadGuard();
  const turnStore = new TurnStore({ queryClient, startChatTurn, speech: createTurnSpeech() });
  const session: AppSession = { account, signOut: () => signOut(signOutDependencies()) };
  page.show(
    <AppProviders
      msal={msal}
      queryClient={queryClient}
      config={config}
      session={session}
      turnStore={turnStore}
      router={env.createRouter()}
    />,
  );
}

/** Runs start-up into `container`; resolves with its React root once the page shows its first
 * settled state. */
export async function start(
  container: HTMLElement,
  env: StartupEnvironment = browserEnvironment(),
): Promise<Root> {
  const root = createRoot(container);
  const page = new Page(root);
  if (!env.hasRequiredApis()) {
    page.show(<UnsupportedBrowserScreen />);
    return root;
  }
  page.show(<StartingScreen />);
  const outcome = await runStartup(env);
  const signOutBeforeApp = (msal: IPublicClientApplication, config: AppConfig) => () =>
    void signOut({
      msal,
      account: null,
      channel: null,
      queryClient: null,
      showSignedOut: () => {
        showSignedOut(msal, config, env, page);
      },
    });
  switch (outcome.kind) {
    case "config_error":
      page.show(<ConfigErrorScreen onReload={env.reload} />);
      return root;
    case "signin_failed": {
      const { msal, config } = outcome;
      page.show(
        <SignInFailedScreen
          referenceId={outcome.referenceId}
          onRetry={() => {
            // Without an instance there is nothing to sign in with until the page loads again.
            if (msal === null) env.reload();
            else loginRedirect(msal, config, env, page);
          }}
        />,
      );
      return root;
    }
    case "not_assigned":
      // The sign-in error names no account; a cached one of this tenant is the best guess.
      showNotAssigned(
        outcome.msal,
        outcome.config,
        pickAccount(outcome.msal, outcome.config.tenantId, null),
        signOutBeforeApp(outcome.msal, outcome.config),
        page,
      );
      return root;
    case "consent_missing":
      page.show(
        <ConsentMissingScreen
          referenceId={outcome.referenceId}
          onSignOut={signOutBeforeApp(outcome.msal, outcome.config)}
        />,
      );
      return root;
    case "signed_out":
      showSignedOut(outcome.msal, outcome.config, env, page);
      return root;
    case "sign_in_required":
      page.show(<RedirectingScreen />);
      loginRedirect(outcome.msal, outcome.config, env, page);
      return root;
    case "ready":
      startApp(outcome, env, page);
      return root;
  }
}
