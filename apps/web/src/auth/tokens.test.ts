import {
  AuthError,
  CacheLookupPolicy,
  InteractionRequiredAuthError,
  type AuthenticationResult,
} from "@azure/msal-browser";
import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, test, vi } from "vitest";

import { ApiFailure, type ApiError } from "../api/problem";
import { TEST_ACCOUNT, fakeMsal, type FakeMsal } from "../test/fakeMsal";
import { TEST_CONFIG } from "../test/handlers";
import {
  currentReturnPath,
  expireSession,
  getAccessToken,
  initTokens,
  isSessionExpired,
  markShellRendered,
  renewAccessToken,
  signInAgain,
  subscribeSessionExpired,
  useSessionExpired,
} from "./tokens";

let instance: FakeMsal;

beforeEach(() => {
  const fake = fakeMsal();
  instance = fake.instance;
  initTokens({ msal: fake.msal, account: TEST_ACCOUNT, scope: TEST_CONFIG.apiScope });
  window.history.replaceState(null, "", "/chat/abc?project=1");
});

async function failureOf(promise: Promise<unknown>): Promise<ApiError> {
  try {
    await promise;
  } catch (reason) {
    if (reason instanceof ApiFailure) return reason.error;
    throw reason;
  }
  throw new Error("resolved");
}

const settled = async (promise: Promise<unknown>) => {
  let done = false;
  void promise.then(
    () => (done = true),
    () => (done = true),
  );
  for (let index = 0; index < 10; index += 1) await Promise.resolve();
  return done;
};

describe("getting tokens", () => {
  test("a token comes from acquireTokenSilent with the one scope and the refresh-token policy", async () => {
    await expect(getAccessToken()).resolves.toBe("access-token");
    expect(instance.acquireTokenSilent).toHaveBeenCalledWith({
      scopes: [TEST_CONFIG.apiScope],
      account: TEST_ACCOUNT,
      cacheLookupPolicy: CacheLookupPolicy.AccessTokenAndRefreshToken,
    });
  });

  test("a pending silent request is shared", async () => {
    const first = getAccessToken();
    const second = getAccessToken();
    expect(second).toBe(first);
    await first;
    expect(instance.acquireTokenSilent).toHaveBeenCalledTimes(1);
    await getAccessToken();
    expect(instance.acquireTokenSilent).toHaveBeenCalledTimes(2);
  });

  test("T-354 before the shell renders, a token that needs interaction redirects at once and never resolves", async () => {
    instance.acquireTokenSilent.mockRejectedValue(
      new InteractionRequiredAuthError("interaction_required", "c"),
    );
    const first = getAccessToken();
    expect(await settled(first)).toBe(false);
    const second = renewAccessToken();
    expect(await settled(second)).toBe(false);
    expect(instance.acquireTokenRedirect).toHaveBeenCalledTimes(1);
    expect(instance.acquireTokenRedirect).toHaveBeenCalledWith({
      scopes: [TEST_CONFIG.apiScope],
      account: TEST_ACCOUNT,
      state: "/chat/abc?project=1",
    });
    expect(isSessionExpired()).toBe(false);
  });

  test("a redirect that can't start may be tried again", async () => {
    instance.acquireTokenSilent.mockRejectedValue(
      new InteractionRequiredAuthError("interaction_required", "c"),
    );
    instance.acquireTokenRedirect.mockRejectedValueOnce(new Error("blocked"));
    void getAccessToken();
    await settled(Promise.resolve());
    await Promise.resolve();
    initTokens({ msal: instance as never, account: TEST_ACCOUNT, scope: TEST_CONFIG.apiScope });
    void getAccessToken();
    await settled(Promise.resolve());
    expect(instance.acquireTokenRedirect).toHaveBeenCalledTimes(2);
  });

  test("T-354 after the shell renders, the same condition sets the session-expired state", async () => {
    markShellRendered();
    const listener = vi.fn();
    const unsubscribe = subscribeSessionExpired(listener);
    instance.acquireTokenSilent.mockRejectedValueOnce(
      new InteractionRequiredAuthError("interaction_required", "c"),
    );
    expect(await failureOf(getAccessToken())).toMatchObject({
      kind: "session_expired",
      referenceId: null,
    });
    expect(isSessionExpired()).toBe(true);
    expect(listener).toHaveBeenCalledTimes(1);
    expect(instance.acquireTokenRedirect).not.toHaveBeenCalled();
    // Every new call fails at once.
    expect((await failureOf(getAccessToken())).kind).toBe("session_expired");
    expect((await failureOf(renewAccessToken())).kind).toBe("session_expired");
    expect(instance.acquireTokenSilent).toHaveBeenCalledTimes(1);
    expireSession();
    expect(listener).toHaveBeenCalledTimes(1);
    unsubscribe();
  });

  test("any other MSAL error is network, with MSAL's correlation ID", async () => {
    instance.acquireTokenSilent.mockRejectedValueOnce(
      new AuthError("endpoints_resolution_error", "corr-7"),
    );
    expect(await failureOf(getAccessToken())).toMatchObject({
      kind: "network",
      referenceId: "corr-7",
    });
    instance.acquireTokenSilent.mockRejectedValueOnce(new AuthError("x", ""));
    expect((await failureOf(getAccessToken())).referenceId).toBeNull();
    instance.acquireTokenSilent.mockRejectedValueOnce(new TypeError("offline"));
    expect((await failureOf(getAccessToken())).referenceId).toBeNull();
    expect(instance.acquireTokenRedirect).not.toHaveBeenCalled();
  });

  test("T-354 after a 401 the token is renewed from the refresh token, skipping the cache", async () => {
    instance.acquireTokenSilent.mockResolvedValueOnce({
      accessToken: "renewed",
    } as AuthenticationResult);
    const first = renewAccessToken();
    expect(renewAccessToken()).toBe(first);
    await expect(first).resolves.toBe("renewed");
    expect(instance.acquireTokenSilent).toHaveBeenCalledWith(
      expect.objectContaining({ cacheLookupPolicy: CacheLookupPolicy.RefreshToken }),
    );
  });

  test("Sign in on the Session expired bar returns to the same screen", async () => {
    await signInAgain();
    expect(instance.acquireTokenRedirect).toHaveBeenCalledWith({
      scopes: [TEST_CONFIG.apiScope],
      account: TEST_ACCOUNT,
      state: "/chat/abc?project=1",
    });
    expect(currentReturnPath()).toBe("/chat/abc?project=1");
  });

  test("the session-expired state is React state", () => {
    const { result } = renderHook(() => useSessionExpired());
    expect(result.current).toBe(false);
    act(() => {
      expireSession();
    });
    expect(result.current).toBe(true);
  });
});
