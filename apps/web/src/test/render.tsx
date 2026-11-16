// Render helpers: the app's providers around a component, or the whole routed app, with the HTTP
// module configured against the MSW server.

import { FluentProvider, webLightTheme } from "@fluentui/react-components";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, type RenderResult } from "@testing-library/react";
import userEvent, { type UserEvent } from "@testing-library/user-event";
import type { ReactElement, ReactNode } from "react";
import { RouterProvider, createMemoryRouter, type RouteObject } from "react-router";
import { vi } from "vitest";

import { configureHttp, startChatTurn, type HttpDependencies } from "../api/http";
import { GC_TIME_MS, FRESHNESS } from "../api/queryClient";
import { LiveRegions } from "../a11y/LiveRegions";
import { SessionContext, type AppSession } from "../auth/session";
import { ConfigContext } from "../bootstrap/loadConfig";
import { TurnStore, TurnStoreContext, type TurnSpeech } from "../chat/stream/turnStore";
import { CurrentUserContext, type CurrentUser } from "../shell/AppShell";
import { appRoutes } from "../shell/routes";
import { TEST_ACCOUNT } from "./fakeMsal";
import { API_BASE, TEST_CONFIG } from "./handlers";

/** A query client with the app's freshness but no automatic retries, so a failure shows at
 * once; the retry policy has its own unit tests. */
export function createTestQueryClient(): QueryClient {
  const client = new QueryClient({
    defaultOptions: { queries: { gcTime: GC_TIME_MS, retry: false }, mutations: { retry: false } },
  });
  for (const { queryKey, staleTime, refetchOnWindowFocus } of FRESHNESS) {
    client.setQueryDefaults(queryKey, { staleTime, refetchOnWindowFocus });
  }
  return client;
}

/** Configures `http.ts` with a fixed token and spies for its signals. */
export function configureTestHttp(overrides: Partial<HttpDependencies> = {}): HttpDependencies {
  const dependencies: HttpDependencies = {
    apiBaseUrl: API_BASE,
    getToken: vi.fn(() => Promise.resolve("test-token")),
    renewToken: vi.fn(() => Promise.resolve("renewed-token")),
    onSessionExpired: vi.fn(),
    onRoleNotAssigned: vi.fn(),
    onAdminRequired: vi.fn(),
    ...overrides,
  };
  configureHttp(dependencies);
  return dependencies;
}

export interface Harness {
  readonly queryClient: QueryClient;
  readonly turnStore: TurnStore;
  readonly session: AppSession;
  readonly user: UserEvent;
  readonly router: ReturnType<typeof createMemoryRouter>;
  readonly result: RenderResult;
}

export interface HarnessOptions {
  /** The initial location. */
  readonly route?: string;
  readonly queryClient?: QueryClient;
  readonly session?: Partial<AppSession>;
  /** Reads streamed text aloud, as `Startup.tsx` wires it. */
  readonly speech?: TurnSpeech;
  /** `getCurrentUser`'s answer, for a part of a screen rendered without the shell. */
  readonly currentUser?: CurrentUser;
}

function Providers({
  children,
  queryClient,
  session,
  turnStore,
}: {
  readonly children: ReactNode;
  readonly queryClient: QueryClient;
  readonly session: AppSession;
  readonly turnStore: TurnStore;
}) {
  return (
    <FluentProvider theme={webLightTheme}>
      <QueryClientProvider client={queryClient}>
        <ConfigContext.Provider value={TEST_CONFIG}>
          <SessionContext.Provider value={session}>
            <TurnStoreContext.Provider value={turnStore}>{children}</TurnStoreContext.Provider>
          </SessionContext.Provider>
        </ConfigContext.Provider>
      </QueryClientProvider>
      <LiveRegions />
    </FluentProvider>
  );
}

/** Renders `routes` inside the app's providers, at `options.route`. */
export function renderRoutes(routes: RouteObject[], options: HarnessOptions = {}): Harness {
  const queryClient = options.queryClient ?? createTestQueryClient();
  const turnStore = new TurnStore({
    queryClient,
    startChatTurn,
    ...(options.speech === undefined ? {} : { speech: options.speech }),
  });
  const session: AppSession = {
    account: TEST_ACCOUNT,
    signOut: vi.fn(() => Promise.resolve()),
    ...options.session,
  };
  const router = createMemoryRouter(routes, { initialEntries: [options.route ?? "/"] });
  const user = userEvent.setup();
  const page = <RouterProvider router={router} />;
  const result = render(
    <Providers queryClient={queryClient} session={session} turnStore={turnStore}>
      {options.currentUser === undefined ? (
        page
      ) : (
        <CurrentUserContext.Provider value={options.currentUser}>
          {page}
        </CurrentUserContext.Provider>
      )}
    </Providers>,
  );
  return { queryClient, turnStore, session, user, router, result };
}

/** Renders `ui` as the only route, inside the app's providers. */
export function renderWithProviders(
  ui: ReactElement,
  options: HarnessOptions & { readonly path?: string } = {},
): Harness {
  return renderRoutes([{ path: options.path ?? "*", element: ui }], options);
}

/** Renders the routed app (shell and screens) at `options.route`. */
export function renderApp(options: HarnessOptions = {}): Harness {
  return renderRoutes(appRoutes, options);
}
