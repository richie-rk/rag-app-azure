import { act, screen, waitFor, within } from "@testing-library/react";
import { HttpResponse, delay, http } from "msw";
import { afterEach, describe, expect, test, vi } from "vitest";

import { expireSession, initTokens } from "../auth/tokens";
import { abortAllWork, registerActiveWork } from "../platform/activeWork";
import { TEST_ACCOUNT, fakeMsal } from "../test/fakeMsal";
import { ADMIN_USER, IDS, TEST_CONFIG, apiUrl, problemResponse } from "../test/handlers";
import { setViewportWidth } from "../test/media";
import { configureTestHttp, renderApp } from "../test/render";
import { server } from "../test/server";

afterEach(() => {
  abortAllWork();
});

function signedIn() {
  configureTestHttp();
  initTokens({ msal: fakeMsal().msal, account: TEST_ACCOUNT, scope: TEST_CONFIG.apiScope });
}

describe("the app shell", () => {
  test("T-351 shows Starting until getCurrentUser succeeds, then the shell", async () => {
    signedIn();
    server.use(
      http.get(apiUrl("/v1/me"), async () => {
        await delay(30);
        return HttpResponse.json(ADMIN_USER);
      }),
    );
    renderApp({ route: "/chat" });
    expect(screen.getByText("Starting DocChat…")).toBeInTheDocument();
    expect(
      await screen.findByRole("button", { name: "Account: Grace Hopper" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("main")).toBeInTheDocument();
    expect(screen.getByRole("main")).toHaveAttribute("tabindex", "-1");
    expect(screen.getByRole("navigation", { name: "Main navigation" })).toBeInTheDocument();
    expect(screen.getByText("Administration")).toBeInTheDocument();
  });

  test("T-351 a failed getCurrentUser shows Couldn't load your account, whose Retry refetches", async () => {
    signedIn();
    let calls = 0;
    server.use(
      http.get(apiUrl("/v1/me"), () => {
        calls += 1;
        return calls === 1
          ? new HttpResponse(null, { status: 403 })
          : HttpResponse.json(ADMIN_USER);
      }),
    );
    const { user, session } = renderApp({ route: "/chat" });
    expect(
      await screen.findByRole("heading", { name: "We couldn't load your account" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/Reference ID: [0-9a-f]{32}/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Sign out" }));
    expect(session.signOut).toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(
      await screen.findByRole("button", { name: "Account: Grace Hopper" }),
    ).toBeInTheDocument();
  });

  test("T-351 403 role_not_assigned leaves the page to the app root's Not assigned", async () => {
    signedIn();
    server.use(http.get(apiUrl("/v1/me"), () => problemResponse(403, "role_not_assigned")));
    renderApp({ route: "/chat" });
    await act(async () => {
      await delay(30);
    });
    expect(screen.getByText("Starting DocChat…")).toBeInTheDocument();
    expect(screen.queryByText("We couldn't load your account")).toBeNull();
  });

  test("the skip link is the first focusable element and moves focus to main", async () => {
    signedIn();
    const { user } = renderApp({ route: "/chat" });
    await screen.findByRole("button", { name: /Account:/ });
    // Fluent's focus manager adds hidden helper elements, which jsdom's tabbing lands on, so the
    // order is read from the document instead.
    const focusable = [
      ...document.querySelectorAll<HTMLElement>(
        "a[href], button, input, textarea, select, [tabindex='0']",
      ),
    ].filter((element) => !element.hasAttribute("data-tabster-dummy"));
    const skip = screen.getByRole("link", { name: "Skip to main content" });
    expect(focusable[0]).toBe(skip);
    await user.click(skip);
    expect(screen.getByRole("main")).toHaveFocus();
  });

  test("T-354 the Session expired bar sits above the screen in main", async () => {
    signedIn();
    renderApp({ route: "/chat" });
    await screen.findByRole("button", { name: /Account:/ });
    act(() => {
      expireSession();
    });
    expect(
      within(screen.getByRole("main")).getByText("Your sign-in has expired"),
    ).toBeInTheDocument();
  });

  test("from 1,280 px the drawer is inline and open, and toggling it is remembered", async () => {
    signedIn();
    const { user } = renderApp({ route: "/chat" });
    await user.click(await screen.findByRole("button", { name: "Hide navigation" }));
    expect(window.localStorage.getItem("docchat.nav.open")).toBe("false");
    expect(screen.getByRole("button", { name: "Show navigation" })).toBeInTheDocument();
  });

  test("a remembered closed drawer starts closed", async () => {
    signedIn();
    window.localStorage.setItem("docchat.nav.open", "false");
    renderApp({ route: "/chat" });
    expect(await screen.findByRole("button", { name: "Show navigation" })).toBeInTheDocument();
  });

  test("below 1,280 px the drawer is an overlay, closed by default and closed after navigating", async () => {
    signedIn();
    setViewportWidth(1_000);
    const { user, router } = renderApp({ route: "/chat" });
    await user.click(await screen.findByRole("button", { name: "Show navigation" }));
    await user.click(await screen.findByRole("link", { name: "Memory" }));
    expect(router.state.location.pathname).toBe("/memory");
    expect(window.localStorage.getItem("docchat.nav.open")).toBeNull();
    expect(await screen.findByRole("button", { name: "Show navigation" })).toBeInTheDocument();
  });

  test("Recent chats show the newest sessions of ['sessions']", async () => {
    signedIn();
    server.use(
      http.get(apiUrl("/v1/sessions"), () =>
        HttpResponse.json({
          items: [
            {
              sessionId: IDS.session,
              projectId: IDS.projectA,
              title: "Budget questions",
              createdAt: "2026-11-01T10:00:00Z",
              lastTurnAt: "2026-11-02T10:00:00Z",
              turnCount: 2,
            },
          ],
          nextCursor: null,
        }),
      ),
    );
    renderApp({ route: "/chat" });
    expect(await screen.findByRole("link", { name: "Budget questions" })).toHaveAttribute(
      "href",
      `/chat/${IDS.session}`,
    );
  });

  test("Recent chats that fail to load say so", async () => {
    signedIn();
    server.use(
      http.get(apiUrl("/v1/sessions"), () => problemResponse(503, "dependency_unavailable")),
    );
    renderApp({ route: "/chat" });
    expect(await screen.findByText("Recent chats didn't load.")).toBeInTheDocument();
  });

  test("T-356 Sign out without running work signs out at once", async () => {
    signedIn();
    const { user, session } = renderApp({ route: "/chat" });
    await user.click(await screen.findByRole("button", { name: /Account:/ }));
    await user.click(await screen.findByRole("menuitem", { name: "Sign out" }));
    expect(session.signOut).toHaveBeenCalled();
  });

  test("T-356 with work running, Sign out asks first; Stay signed in leaves it running", async () => {
    signedIn();
    const abort = vi.fn();
    registerActiveWork({ abort });
    const { user, session } = renderApp({ route: "/chat" });
    await user.click(await screen.findByRole("button", { name: /Account:/ }));
    await user.click(await screen.findByRole("menuitem", { name: "Sign out" }));
    expect(session.signOut).not.toHaveBeenCalled();
    const dialog = await screen.findByRole("dialog", { name: "Sign out now?" });
    expect(dialog).toHaveTextContent(
      "An answer or upload is still in progress. Signing out stops it.",
    );
    await user.click(await screen.findByRole("button", { name: "Stay signed in" }));
    expect(session.signOut).not.toHaveBeenCalled();
    expect(abort).not.toHaveBeenCalled();
    // The page stays inert until the modal dialog has gone.
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    await user.click(screen.getByRole("button", { name: /Account:/ }));
    await user.click(await screen.findByRole("menuitem", { name: "Sign out" }));
    // The confirmation's own button; the menu's item is a menuitem.
    await user.click(await screen.findByRole("button", { name: "Sign out" }));
    expect(session.signOut).toHaveBeenCalled();
  });

  test("the user is read from the shell's context only", async () => {
    const { useCurrentUser } = await import("./AppShell");
    const { renderHook } = await import("@testing-library/react");
    expect(() => renderHook(() => useCurrentUser())).toThrow("useCurrentUser");
  });
});
