import { QueryClient } from "@tanstack/react-query";
import { renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, test, vi } from "vitest";

import { abortAllWork, registerActiveWork } from "../platform/activeWork";
import { readSignedOut } from "../platform/storage";
import { TEST_ACCOUNT, fakeMsal } from "../test/fakeMsal";
import { configureTestHttp } from "../test/render";
import { request } from "../api/http";
import {
  SessionContext,
  signOut,
  signedOutElsewhere,
  useSession,
  type AppSession,
} from "./session";

function dependencies() {
  const { instance, msal } = fakeMsal();
  const queryClient = new QueryClient();
  queryClient.setQueryData(["me"], { displayName: "x" });
  const channel = { postSignedOut: vi.fn(), close: vi.fn() };
  const showSignedOut = vi.fn();
  const work = vi.fn();
  registerActiveWork({ abort: work });
  return {
    instance,
    deps: { msal, account: TEST_ACCOUNT, channel, queryClient, showSignedOut },
    work,
  };
}

describe("signing out", () => {
  test("T-356 Sign out sets the flag, tells the other tabs, aborts work, clears the cache and logs out", async () => {
    configureTestHttp();
    const { instance, deps, work } = dependencies();
    await signOut(deps);
    expect(readSignedOut()).toBe(true);
    expect(deps.channel.postSignedOut).toHaveBeenCalled();
    expect(work).toHaveBeenCalled();
    expect(deps.queryClient.getQueryData(["me"])).toBeUndefined();
    expect(instance.logoutRedirect).toHaveBeenCalledWith({
      account: TEST_ACCOUNT,
      postLogoutRedirectUri: `${window.location.origin}/`,
    });
    await expect(request("getCurrentUser", {})).rejects.toThrow("aborted");
    expect(deps.showSignedOut).not.toHaveBeenCalled();
    abortAllWork();
  });

  test("T-356 a logout that can't start clears the cache and shows Signed out", async () => {
    const { instance, deps } = dependencies();
    instance.logoutRedirect.mockRejectedValueOnce(new Error("blocked"));
    instance.clearCache.mockRejectedValueOnce(new Error("also blocked"));
    await signOut({ ...deps, account: null, channel: null, queryClient: null });
    expect(instance.logoutRedirect).toHaveBeenCalledWith({
      postLogoutRedirectUri: `${window.location.origin}/`,
    });
    expect(instance.clearCache).toHaveBeenCalled();
    expect(deps.showSignedOut).toHaveBeenCalled();
  });

  test("T-356 a sign-out in another tab clears this tab's tokens and shows Signed out", async () => {
    const { instance, deps, work } = dependencies();
    instance.clearCache.mockRejectedValueOnce(new Error("blocked"));
    await signedOutElsewhere(deps);
    expect(work).toHaveBeenCalled();
    expect(deps.queryClient.getQueryData(["me"])).toBeUndefined();
    expect(instance.clearCache).toHaveBeenCalled();
    expect(readSignedOut()).toBe(true);
    expect(deps.showSignedOut).toHaveBeenCalled();
    expect(deps.channel.postSignedOut).not.toHaveBeenCalled();
    await signedOutElsewhere({ ...deps, queryClient: null });
  });

  test("the session is read from context inside the app only", () => {
    const session: AppSession = { account: TEST_ACCOUNT, signOut: () => Promise.resolve() };
    const wrapper = ({ children }: { children: ReactNode }) => (
      <SessionContext.Provider value={session}>{children}</SessionContext.Provider>
    );
    expect(renderHook(() => useSession(), { wrapper }).result.current).toBe(session);
    expect(() => renderHook(() => useSession())).toThrow("useSession");
  });
});
