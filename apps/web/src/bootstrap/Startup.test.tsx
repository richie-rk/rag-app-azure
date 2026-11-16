import {
  AuthError,
  InteractionRequiredAuthError,
  type AuthenticationResult,
} from "@azure/msal-browser";
import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import type { Root } from "react-dom/client";
import { createMemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { abortAllWork } from "../platform/activeWork";
import { readSignedOut, setSignedOut } from "../platform/storage";
import { appRoutes } from "../shell/routes";
import { present } from "../test/chat";
import { TEST_ACCOUNT, fakeMsal } from "../test/fakeMsal";
import { ADMIN_USER, TEST_CONFIG, apiUrl, problemResponse } from "../test/handlers";
import { server } from "../test/server";
import { hasRequiredBrowserApis, runStartup, start, type StartupEnvironment } from "./Startup";

type ActGlobal = typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean | undefined };

let container: HTMLElement;
let root: Root | null = null;
let actEnvironment: boolean | undefined;

beforeEach(() => {
  // Start-up renders into its own root outside act(), as the page does.
  actEnvironment = (globalThis as ActGlobal).IS_REACT_ACT_ENVIRONMENT;
  (globalThis as ActGlobal).IS_REACT_ACT_ENVIRONMENT = false;
  container = document.createElement("div");
  document.body.append(container);
});

afterEach(() => {
  const current = root;
  if (current !== null) {
    act(() => {
      current.unmount();
    });
  }
  root = null;
  container.remove();
  abortAllWork();
  (globalThis as ActGlobal).IS_REACT_ACT_ENVIRONMENT = actEnvironment;
});

function environment(overrides: Partial<StartupEnvironment> = {}, accounts = [TEST_ACCOUNT]) {
  const { instance, msal } = fakeMsal(accounts);
  const signedOutElsewhere: (() => void)[] = [];
  const channel = { postSignedOut: vi.fn(), close: vi.fn() };
  const env: StartupEnvironment = {
    hasRequiredApis: () => true,
    loadConfig: vi.fn(() => Promise.resolve(TEST_CONFIG)),
    createMsal: vi.fn(() => Promise.resolve(msal)),
    location: { origin: "https://docchat.example", pathname: "/memory", search: "?x=1" },
    replacePath: vi.fn(),
    reload: vi.fn(),
    openChannel: vi.fn((onSignedOut: () => void) => {
      signedOutElsewhere.push(onSignedOut);
      return channel;
    }),
    createRouter: () => createMemoryRouter(appRoutes, { initialEntries: ["/chat"] }),
    ...overrides,
  };
  return { env, instance, msal, channel, signedOutElsewhere };
}

async function run(env: StartupEnvironment): Promise<void> {
  root = await start(container, env);
}

const page = () => within(container);

describe("start-up", () => {
  test("hasRequiredBrowserApis needs every API of section 1.9", () => {
    const full = {
      ReadableStream,
      TextDecoder,
      crypto: { subtle: {}, randomUUID: () => "x" },
      BroadcastChannel: vi.fn(),
      AbortController,
    } as unknown as typeof globalThis;
    expect(hasRequiredBrowserApis(full)).toBe(true);
    for (const missing of [
      "ReadableStream",
      "TextDecoder",
      "BroadcastChannel",
      "AbortController",
    ]) {
      expect(hasRequiredBrowserApis({ ...full, [missing]: undefined }), missing).toBe(false);
    }
    expect(hasRequiredBrowserApis({ ...full, crypto: { randomUUID: () => "x" } } as never)).toBe(
      false,
    );
    expect(hasRequiredBrowserApis({ ...full, crypto: { subtle: {} } } as never)).toBe(false);
    expect(hasRequiredBrowserApis({ ...full, crypto: undefined } as never)).toBe(false);
  });

  test("a missing browser API shows Unsupported browser and makes no network call", async () => {
    const { env } = environment({ hasRequiredApis: () => false });
    await run(env);
    expect(
      await page().findByRole("heading", { name: "This browser can't run DocChat" }),
    ).toBeInTheDocument();
    expect(env.loadConfig).not.toHaveBeenCalled();
    await waitFor(() => {
      expect(document.title).toBe("This browser can't run DocChat · DocChat");
    });
  });

  test("T-341 a configuration that fails shows the Configuration error screen, whose Reload reloads", async () => {
    const { env } = environment({
      loadConfig: () => Promise.reject(new Error("config_unavailable")),
    });
    await run(env);
    await userEvent.click(await page().findByRole("button", { name: "Reload" }));
    expect(page().getByRole("heading", { name: "DocChat can't start" })).toBeInTheDocument();
    expect(env.reload).toHaveBeenCalled();
  });

  test("the viewport-high page box sits inside the provider, whose class names Fluent copies to every portal", async () => {
    const { env } = environment({
      loadConfig: () => Promise.reject(new Error("config_unavailable")),
    });
    await run(env);
    await page().findByRole("heading", { name: "DocChat can't start" });
    const provider = present(container.querySelector(".fui-FluentProvider"));
    const box = present(provider.firstElementChild);
    const viewportHigh = `${String(window.innerHeight)}px`;
    expect(getComputedStyle(box).minHeight).toBe(viewportHigh);
    expect(getComputedStyle(provider).minHeight).not.toBe(viewportHigh);
  });

  test("an MSAL instance that can't be created shows Sign-in failed without a reference ID", async () => {
    const { env } = environment({ createMsal: () => Promise.reject(new Error("bad")) });
    await run(env);
    await userEvent.click(await page().findByRole("button", { name: "Retry" }));
    expect(page().queryByText(/Reference ID/)).toBeNull();
    expect(env.reload).toHaveBeenCalled();
  });

  test("T-350 another sign-in error shows Sign-in failed with the reference ID, and Retry signs in", async () => {
    const { env, instance } = environment();
    instance.handleRedirectPromise.mockRejectedValueOnce(
      new AuthError("server_error", "corr-1", "AADSTS50011"),
    );
    await run(env);
    expect(
      await page().findByRole("heading", { name: "Sign-in didn't finish" }),
    ).toBeInTheDocument();
    expect(page().getByText("Reference ID: corr-1")).toBeInTheDocument();
    instance.loginRedirect.mockRejectedValueOnce(new AuthError("popup_window_error", "corr-2"));
    await userEvent.click(page().getByRole("button", { name: "Retry" }));
    expect(instance.loginRedirect).toHaveBeenCalledWith({
      scopes: [TEST_CONFIG.apiScope],
      state: "/memory?x=1",
    });
    expect(await page().findByText("Reference ID: corr-2")).toBeInTheDocument();
  });

  test("T-350 AADSTS50105 shows Not assigned with the cached account; Retry picks another account", async () => {
    const { env, instance } = environment();
    instance.handleRedirectPromise.mockRejectedValueOnce(
      new AuthError("access_denied", "c", "AADSTS50105"),
    );
    await run(env);
    expect(
      await page().findByRole("heading", { name: "You don't have access to DocChat yet" }),
    ).toBeInTheDocument();
    expect(page().getByText(/Your account, ada@example.test, isn't set up/)).toBeInTheDocument();
    await userEvent.click(page().getByRole("button", { name: "Retry" }));
    expect(instance.loginRedirect).toHaveBeenCalledWith({
      scopes: [TEST_CONFIG.apiScope],
      prompt: "select_account",
    });
    await userEvent.click(page().getByRole("button", { name: "Sign out" }));
    expect(instance.logoutRedirect).toHaveBeenCalledWith({
      postLogoutRedirectUri: `${window.location.origin}/`,
    });
    expect(readSignedOut()).toBe(true);
  });

  test("Not assigned without a cached account names none", async () => {
    const { env, instance } = environment({}, []);
    instance.handleRedirectPromise.mockRejectedValueOnce(
      new AuthError("access_denied", "c", "AADSTS50105"),
    );
    await run(env);
    expect(await page().findByText(/Your account, , isn't set up/)).toBeInTheDocument();
  });

  test("T-350 AADSTS65001 shows Consent missing with the reference ID; a failed sign-out shows Signed out", async () => {
    const { env, instance } = environment();
    instance.handleRedirectPromise.mockRejectedValueOnce(
      new AuthError("invalid_grant", "corr-9", "AADSTS65001"),
    );
    await run(env);
    expect(
      await page().findByRole("heading", {
        name: "DocChat isn't set up for your organization yet",
      }),
    ).toBeInTheDocument();
    expect(page().getByText("Reference ID: corr-9")).toBeInTheDocument();
    instance.logoutRedirect.mockRejectedValueOnce(new Error("blocked"));
    await userEvent.click(page().getByRole("button", { name: "Sign out" }));
    expect(await page().findByRole("heading", { name: "You're signed out" })).toBeInTheDocument();
    expect(instance.clearCache).toHaveBeenCalled();
  });

  test("T-350 a cancelled sign-in shows Signed out, whose Sign in removes the flag and signs in", async () => {
    const { env, instance } = environment();
    setSignedOut();
    instance.handleRedirectPromise.mockRejectedValueOnce(new AuthError("user_cancelled", "c"));
    await run(env);
    await userEvent.click(await page().findByRole("button", { name: "Sign in" }));
    expect(readSignedOut()).toBe(false);
    expect(instance.loginRedirect).toHaveBeenCalledWith({
      scopes: [TEST_CONFIG.apiScope],
      state: "/memory?x=1",
    });
  });

  test("T-349 with no account and no flag, start-up redirects to sign-in with the return path", async () => {
    const { env, instance } = environment({}, []);
    await run(env);
    expect(await page().findByText("Taking you to sign-in…")).toBeInTheDocument();
    expect(instance.loginRedirect).toHaveBeenCalledWith({
      scopes: [TEST_CONFIG.apiScope],
      state: "/memory?x=1",
    });
  });

  test("with no account and the signed-out flag, start-up shows Signed out", async () => {
    const { env, instance } = environment({}, []);
    setSignedOut();
    await run(env);
    expect(await page().findByRole("heading", { name: "You're signed out" })).toBeInTheDocument();
    expect(instance.loginRedirect).not.toHaveBeenCalled();
  });
});

describe("runStartup", () => {
  test("T-349 a redirect result sets the account, clears the flag and returns to its safe state", async () => {
    const { env, instance } = environment();
    setSignedOut();
    const resultAccount = { ...TEST_ACCOUNT, homeAccountId: "result" };
    instance.handleRedirectPromise.mockResolvedValueOnce({
      account: resultAccount,
      state: "//evil.example",
    } as AuthenticationResult);
    const outcome = await runStartup(env);
    expect(outcome).toMatchObject({ kind: "ready", account: resultAccount });
    expect(instance.handleRedirectPromise).toHaveBeenCalledWith({
      navigateToLoginRequestUrl: false,
    });
    expect(env.replacePath).toHaveBeenCalledWith("/chat");
    expect(instance.setActiveAccount).toHaveBeenCalledWith(resultAccount);
    expect(readSignedOut()).toBe(false);
  });

  test("T-349 a redirect result's valid state is kept", async () => {
    const { env, instance } = environment();
    instance.handleRedirectPromise.mockResolvedValueOnce({
      account: TEST_ACCOUNT,
      state: "/memory",
    } as AuthenticationResult);
    await runStartup(env);
    expect(env.replacePath).toHaveBeenCalledWith("/memory");
  });

  test.each([
    [new AuthError("access_denied", "c", "AADSTS50105"), "not_assigned"],
    [new AuthError("invalid_grant", "c", "AADSTS90094"), "consent_missing"],
    [new AuthError("user_cancelled", "c"), "signed_out"],
    [new InteractionRequiredAuthError("x", "c"), "signin_failed"],
  ])("a redirect error %s leads to %s", async (error, kind) => {
    const { env, instance } = environment();
    instance.handleRedirectPromise.mockRejectedValueOnce(error);
    expect((await runStartup(env)).kind).toBe(kind);
  });
});

describe("the signed-in app", () => {
  test("T-351 the shell renders after getCurrentUser, which carries the silent token", async () => {
    const { env, instance, channel } = environment();
    const authorizations: (string | null)[] = [];
    server.use(
      http.get(apiUrl("/v1/me"), ({ request }) => {
        authorizations.push(request.headers.get("authorization"));
        return HttpResponse.json(ADMIN_USER);
      }),
    );
    await run(env);
    expect(
      await page().findByRole("button", { name: "Account: Grace Hopper" }),
    ).toBeInTheDocument();
    expect(authorizations[0]).toBe("Bearer access-token");
    expect(instance.setActiveAccount).toHaveBeenCalledWith(TEST_ACCOUNT);
    expect(instance.acquireTokenSilent).toHaveBeenCalledWith(
      expect.objectContaining({ scopes: [TEST_CONFIG.apiScope], account: TEST_ACCOUNT }),
    );
    expect(channel.postSignedOut).not.toHaveBeenCalled();
  });

  test("T-351 403 role_not_assigned from getCurrentUser shows Not assigned with no further call", async () => {
    const { env, instance } = environment();
    let calls = 0;
    server.use(
      http.get(apiUrl("/v1/me"), () => {
        calls += 1;
        return problemResponse(403, "role_not_assigned");
      }),
    );
    await run(env);
    expect(
      await page().findByText(/Your account, ada@example.test, isn't set up/),
    ).toBeInTheDocument();
    expect(calls).toBe(1);
    await userEvent.click(page().getByRole("button", { name: "Sign out" }));
    expect(instance.logoutRedirect).toHaveBeenCalledWith({
      account: TEST_ACCOUNT,
      postLogoutRedirectUri: `${window.location.origin}/`,
    });
  });

  test("T-356 a sign-out in another tab shows Signed out and clears this tab's cache", async () => {
    const { env, instance, signedOutElsewhere } = environment();
    await run(env);
    await page().findByRole("button", { name: /Account:/ });
    act(() => {
      signedOutElsewhere[0]?.();
    });
    expect(await page().findByRole("heading", { name: "You're signed out" })).toBeInTheDocument();
    expect(instance.clearCache).toHaveBeenCalled();
    expect(readSignedOut()).toBe(true);
  });

  test("T-356 Sign out posts to the other tabs and ends the Entra ID session", async () => {
    const { env, instance, channel } = environment();
    await run(env);
    await userEvent.click(await page().findByRole("button", { name: /Account:/ }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Sign out" }));
    await waitFor(() => {
      expect(instance.logoutRedirect).toHaveBeenCalledWith({
        account: TEST_ACCOUNT,
        postLogoutRedirectUri: `${window.location.origin}/`,
      });
    });
    expect(channel.postSignedOut).toHaveBeenCalled();
    expect(readSignedOut()).toBe(true);
  });

  test("T-352 403 admin_required refetches getCurrentUser", async () => {
    const { env } = environment({
      createRouter: () => createMemoryRouter(appRoutes, { initialEntries: ["/admin/projects"] }),
    });
    let meCalls = 0;
    server.use(
      http.get(apiUrl("/v1/me"), () => {
        meCalls += 1;
        return HttpResponse.json(
          meCalls === 1 ? ADMIN_USER : { ...ADMIN_USER, isAdmin: false, roles: ["User"] },
        );
      }),
      http.get(apiUrl("/v1/admin/projects"), () => problemResponse(403, "admin_required")),
    );
    await run(env);
    // The Projects screen's own list call is the admin call refused here.
    expect(
      await page().findByRole("heading", { name: "This page is for administrators" }),
    ).toBeInTheDocument();
    expect(meCalls).toBe(2);
  });
});
