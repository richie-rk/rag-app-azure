// Sign-in, the access states, renewal, the no-iframe rule and sign-out against the stub identity
// provider (10-web-client.md sections 4.1 to 4.9; acceptance criteria 8 to 10 and 13 to 15).

import type { Page } from "@playwright/test";

import { ACCEPT_RETURNED_STATE } from "../../fixtures/web/identity.ts";
import { session, turn, USER_EMAIL } from "../../fixtures/web/stub-api/builders.ts";
import { APP_ORIGIN } from "../../fixtures/web/stub-api/protocol.ts";
import { en, expect, expectAccessible, fill, test } from "../../fixtures/web/test.ts";

/** The part of an authorize request's `state` the app chose: MSAL's own part comes before `|`. */
function userState(url: URL | undefined): string {
  const state = url?.searchParams.get("state") ?? "";
  return state.slice(state.indexOf("|") + 1);
}

function traceIdOf(traceparent: string | undefined): string {
  return (traceparent ?? "").split("-")[1] ?? "";
}

async function sessionStorageKeys(page: Page): Promise<string[]> {
  return page.evaluate(() => Object.keys(window.sessionStorage));
}

const MSAL_CREDENTIAL = /accesstoken|refreshtoken|idtoken/i;

test.describe("the return path (section 4.3)", () => {
  test("T-349 with no account, /memory signs in and returns to the Memory screen", async ({
    app,
    page,
    idp,
  }) => {
    await app.openShell("/memory");
    await expect(app.heading()).toHaveText(en["memory.title"]);
    expect(new URL(page.url()).pathname).toBe("/memory");
    expect(idp.authorizeRequests).toHaveLength(1);
    expect(userState(idp.authorizeRequests[0])).toBe("/memory");
    await expect(page).toHaveTitle(`${en["memory.title"]} · DocChat`);
    await expectAccessible(page, "Memory after the sign-in round trip");
  });

  test("T-349 a return path of //evil.example lands on /chat", async ({ app, page, idp }) => {
    await page.goto(`${APP_ORIGIN}//evil.example`);
    await app.waitForShell();
    expect(userState(idp.authorizeRequests[0])).toBe("//evil.example");
    expect(page.url()).toMatch(new RegExp(`^${APP_ORIGIN}/chat(\\?|$)`));
  });

  test.describe(() => {
    test.use({ identity: { authorize: [{ kind: "tamper", userState: "https://evil.example/" }] } });

    test("T-349 a tampered state of https://evil.example/ lands on /chat", async ({
      app,
      page,
      idp,
    }) => {
      await page.addInitScript(ACCEPT_RETURNED_STATE);
      await app.openShell("/memory");
      expect(userState(idp.authorizeRequests[0])).toBe("/memory");
      expect(page.url()).toMatch(new RegExp(`^${APP_ORIGIN}/chat(\\?|$)`));
    });
  });
});

test.describe("the access states of a sign-in error (section 4.8)", () => {
  test.describe(() => {
    test.use({
      identity: {
        authorize: [
          {
            kind: "error",
            error: "access_denied",
            description:
              "AADSTS50105: Your administrator has configured the application to block users unless they are specifically granted access.",
            errorNo: "50105",
          },
        ],
      },
    });

    test("T-350 AADSTS50105 shows Not assigned", async ({ app, page, stub }) => {
      await app.open("/chat");
      await expect(app.heading()).toHaveText(en["access.notAssigned.title"]);
      await expect(page.getByRole("button", { name: en["access.retry"] })).toBeVisible();
      await expect(page.getByRole("button", { name: en["access.signOut"] })).toBeVisible();
      await expect(page).toHaveTitle(`${en["access.notAssigned.title"]} · DocChat`);
      expect(await stub.requests()).toEqual([]);
      await expectAccessible(page, "Not assigned");
    });
  });

  test.describe(() => {
    test.use({
      identity: {
        authorize: [
          {
            kind: "error",
            error: "invalid_client",
            description:
              "AADSTS65001: The user or administrator has not consented to use the application.",
            errorNo: "65001",
          },
        ],
      },
    });

    test("T-350 AADSTS65001 shows Consent missing with the reference ID", async ({ app, page }) => {
      await app.open("/chat");
      await expect(app.heading()).toHaveText(en["access.consentMissing.title"]);
      await expect(page.getByText(en["access.consentMissing.body"])).toBeVisible();
      await expect(page.getByText(/^Reference ID: \S+$/)).toBeVisible();
      await expect(page.getByRole("button", { name: en["access.signOut"] })).toBeVisible();
      await expectAccessible(page, "Consent missing");
    });
  });

  test.describe(() => {
    test.use({
      identity: {
        authorize: [
          {
            kind: "error",
            error: "access_denied",
            description: "The user has denied access to the scope requested by the client app.",
          },
        ],
      },
    });

    test("T-350 access_denied alone shows Signed out", async ({ app, page }) => {
      await app.open("/chat");
      await expect(app.heading()).toHaveText(en["access.signedOut.title"]);
      await expect(page.getByRole("button", { name: en["access.signedOut.action"] })).toBeVisible();
      await expectAccessible(page, "Signed out");
    });
  });

  test.describe(() => {
    test.use({
      identity: {
        authorize: [
          {
            kind: "error",
            error: "server_error",
            description: "AADSTS90033: A transient error has occurred. Please try again.",
            errorNo: "90033",
          },
        ],
      },
    });

    test("T-350 any other error shows Sign-in failed with the reference ID", async ({
      app,
      page,
      idp,
    }) => {
      await app.open("/chat");
      await expect(app.heading()).toHaveText(en["access.signInFailed.title"]);
      await expect(page.getByText(/^Reference ID: \S+$/)).toBeVisible();
      await expectAccessible(page, "Sign-in failed");
      // Retry signs in once more.
      await page.getByRole("button", { name: en["access.retry"] }).click();
      await app.waitForShell();
      expect(idp.authorizeRequests).toHaveLength(2);
    });
  });
});

test.describe("start-up answers of getCurrentUser (sections 4.2 and 4.5)", () => {
  test("T-351 403 role_not_assigned shows Not assigned and no other call follows", async ({
    app,
    page,
    stub,
  }) => {
    await stub.problem("getCurrentUser", { code: "role_not_assigned" });
    await app.open("/chat");
    await expect(app.heading()).toHaveText(en["access.notAssigned.title"]);
    await expect(
      page.getByText(fill(en["access.notAssigned.body"], { account: USER_EMAIL })),
    ).toBeVisible();
    await expectAccessible(page, "Not assigned after getCurrentUser");
    const calls = await stub.requests();
    expect(calls.map((call) => call.operation)).toEqual(["getCurrentUser"]);
  });

  test("T-351 a bare 403 shows Couldn't load your account with the trace ID and Retry", async ({
    app,
    page,
    stub,
  }) => {
    await stub.script({ operation: "getCurrentUser", answer: { kind: "bare", status: 403 } });
    await app.open("/chat");
    await expect(app.heading()).toHaveText(en["access.loadUserFailed.title"]);
    const [first] = await stub.requests("getCurrentUser");
    const traceId = traceIdOf(first?.headers["traceparent"]);
    expect(traceId).toMatch(/^[0-9a-f]{32}$/);
    await expect(page.getByText(fill(en["error.reference"], { id: traceId }))).toBeVisible();
    await expect(page.getByText(en["error.server.title"])).toBeVisible();
    await expect(page.getByText(en["access.notAssigned.title"])).toHaveCount(0);
    await expectAccessible(page, "Couldn't load your account");
    await page.getByRole("button", { name: en["access.retry"] }).click();
    await app.waitForShell();
  });

  test("T-351 a bare 401 goes through the single refresh-token repeat", async ({
    app,
    stub,
    idp,
  }) => {
    await stub.script({ operation: "getCurrentUser", answer: { kind: "bare", status: 401 } });
    await app.openShell("/chat");
    const calls = await stub.requests("getCurrentUser");
    expect(calls).toHaveLength(2);
    expect(calls[0]?.headers["traceparent"]).not.toBe(calls[1]?.headers["traceparent"]);
    expect(calls[1]?.headers["authorization"]).toBe(`Bearer ${idp.accessToken(2)}`);
    expect(idp.tokenRequests.map((request) => request.grantType)).toEqual([
      "authorization_code",
      "refresh_token",
    ]);
    expect(idp.authorizeRequests).toHaveLength(1);
  });
});

const TWO_SESSIONS = {
  sessionsPageSize: 1,
  sessions: [
    session({ title: "Leave questions", turns: [turn({ question: "How much leave?" })] }),
    session({
      title: "Travel questions",
      turns: [turn({ question: "Who approves travel?" })],
      ageSeconds: 7200,
    }),
  ],
};

test.describe("renewal (sections 4.4 and 4.5)", () => {
  test.describe(() => {
    test.use({ identity: { accessTokenLifetimeSeconds: [120, 3600], refreshExpired: true } });

    test("T-354 an expired refresh token at start-up calls acquireTokenRedirect at once", async ({
      app,
      page,
      stub,
      idp,
    }) => {
      await app.openShell("/memory");
      // The first sign-in's access token is inside MSAL's renewal window and its refresh token is
      // refused, so before the shell renders the client redirects for interaction at once.
      expect(idp.authorizeRequests).toHaveLength(2);
      expect(userState(idp.authorizeRequests[1])).toBe("/memory");
      expect(idp.tokenRequests.map((request) => request.grantType)).toEqual([
        "authorization_code",
        "refresh_token",
        "authorization_code",
      ]);
      // Only the second sign-in's token reached the API.
      const calls = await stub.requests("getCurrentUser");
      expect(calls.map((call) => call.headers["authorization"])).toEqual([
        `Bearer ${idp.accessToken(2)}`,
      ]);
      expect(new URL(page.url()).pathname).toBe("/memory");
    });
  });

  test.describe(() => {
    test.use({ world: TWO_SESSIONS, identity: { refreshExpired: true } });

    test("T-354 after the shell renders, the same condition shows the Session expired bar and keeps the draft", async ({
      app,
      page,
      stub,
      idp,
    }) => {
      await app.openShell("/chat");
      const draft = "A question I haven't sent yet";
      await app.composer().fill(draft);
      await stub.script({
        operation: "listSessions",
        query: { cursor: true },
        answer: { kind: "bare", status: 401 },
      });
      await page.getByRole("button", { name: en["chat.sessions.more"] }).click();
      const bar = page.getByText(en["session.expired.body"]);
      await expect(bar).toBeVisible();
      await expect(app.composer()).toHaveValue(draft);
      // The client never redirects on its own once the shell is on screen.
      expect(idp.authorizeRequests).toHaveLength(1);
      await expectAccessible(page, "Session expired bar");

      // Sign in on the bar: a top-level visit to Entra ID that returns to the same screen.
      const returnPath = new URL(page.url()).pathname + new URL(page.url()).search;
      await page.getByRole("button", { name: en["session.expired.action"] }).click();
      await app.waitForShell();
      await expect(bar).toHaveCount(0);
      expect(idp.authorizeRequests).toHaveLength(2);
      expect(userState(idp.authorizeRequests[1])).toBe(returnPath);
    });
  });

  test.describe(() => {
    test.use({ world: TWO_SESSIONS });

    test("T-354 a 401 followed by 200 after a refresh-token renewal changes nothing on screen", async ({
      app,
      page,
      stub,
      idp,
    }) => {
      await app.openShell("/chat");
      await stub.script({
        operation: "listSessions",
        query: { cursor: true },
        answer: { kind: "bare", status: 401 },
      });
      await page.getByRole("button", { name: en["chat.sessions.more"] }).click();
      await expect(
        app.sessionPanel().getByRole("link", { name: /Travel questions/ }),
      ).toBeVisible();
      await expect(page.getByText(en["session.expired.body"])).toHaveCount(0);
      await expect(page.getByText(en["session.expired.title"])).toHaveCount(0);
      const more = await stub.requests("listSessions");
      expect(more.filter((call) => call.query["cursor"] !== undefined)).toHaveLength(2);
      expect(idp.tokenRequests.map((request) => request.grantType)).toContain("refresh_token");
    });

    test("T-354 two 401s in a row show the Session expired bar", async ({ app, page, stub }) => {
      await app.openShell("/chat");
      await stub.script({
        operation: "listSessions",
        query: { cursor: true },
        times: 2,
        answer: { kind: "bare", status: 401 },
      });
      await page.getByRole("button", { name: en["chat.sessions.more"] }).click();
      await expect(page.getByText(en["session.expired.body"])).toBeVisible();
      await expect(page.getByRole("button", { name: en["session.expired.action"] })).toBeVisible();
    });
  });
});

test.describe(() => {
  test.use({ world: TWO_SESSIONS });

  test("T-355 no authorize request uses prompt=none and no frame loads Entra ID", async ({
    app,
    page,
    stub,
    idp,
    gates,
  }) => {
    await app.openShell("/chat");
    // A renewal after a 401, then a sign-out: the flows that could reach for a hidden frame.
    await stub.script({
      operation: "listSessions",
      query: { cursor: true },
      answer: { kind: "bare", status: 401 },
    });
    await page.getByRole("button", { name: en["chat.sessions.more"] }).click();
    await expect(app.sessionPanel().getByRole("link", { name: /Travel questions/ })).toBeVisible();
    await app.signOut();
    await expect(app.heading()).toHaveText(en["access.signedOut.title"]);
    expect(
      idp.authorizeRequests.filter((url) => url.searchParams.get("prompt") === "none"),
    ).toEqual([]);
    expect(gates.loginFrames).toEqual([]);
    expect(
      page.frames().filter((frame) => frame.url().includes("login.microsoftonline.com")),
    ).toEqual([]);
  });
});

test.describe("signing out (section 4.6)", () => {
  test("T-356 Sign out removes MSAL's entries, calls the logout endpoint and shows Signed out", async ({
    app,
    page,
    idp,
  }) => {
    await app.openShell("/chat");
    expect((await sessionStorageKeys(page)).some((key) => MSAL_CREDENTIAL.test(key))).toBe(true);
    await app.signOut();
    await expect(app.heading()).toHaveText(en["access.signedOut.title"]);
    expect(idp.logoutRequests).toHaveLength(1);
    expect(idp.logoutRequests[0]?.searchParams.get("post_logout_redirect_uri")).toBe(
      `${APP_ORIGIN}/`,
    );
    const keys = await sessionStorageKeys(page);
    expect(keys.filter((key) => MSAL_CREDENTIAL.test(key))).toEqual([]);
    expect(await page.evaluate(() => window.sessionStorage.getItem("docchat.signedOut"))).toBe("1");
    await expectAccessible(page, "Signed out after sign-out");

    // Sign in removes the flag and signs in once more.
    await page.getByRole("button", { name: en["access.signedOut.action"] }).click();
    await app.waitForShell();
    expect(
      await page.evaluate(() => window.sessionStorage.getItem("docchat.signedOut")),
    ).toBeNull();
  });

  test("T-356 a second tab shows Signed out at once and sends no further call", async ({
    app,
    context,
    stub,
  }) => {
    await app.openShell("/chat");
    const second = await context.newPage();
    await second.goto("/chat");
    await expect(second.getByRole("banner")).toBeVisible();
    const before = (await stub.requests()).length;
    await app.signOut();
    await expect(second.getByRole("heading", { level: 1 })).toHaveText(
      en["access.signedOut.title"],
      { timeout: 2000 },
    );
    expect(await second.evaluate(() => window.sessionStorage.getItem("docchat.signedOut"))).toBe(
      "1",
    );
    await expect(app.heading()).toHaveText(en["access.signedOut.title"]);
    await second.waitForTimeout(1500);
    expect(await stub.requests()).toHaveLength(before);
  });

  test("T-356 with a turn streaming, Sign out asks first and Stay signed in leaves it running", async ({
    app,
    page,
    stub,
    idp,
  }) => {
    await app.openShell("/chat?project=0f3e5c2a-9b1d-4e7f-8a6c-5d4b3a291807");
    await stub.stream({ hold: "beforeCitations" });
    const question = "How many days of leave do I get?";
    await app.ask(question);
    const answer = app.turn(question);
    await expect(answer.getByText(/25 days of annual leave/)).toBeVisible();
    await app.signOut();
    const dialog = page.getByRole("dialog", { name: en["shell.signOut.title"] });
    await expect(dialog).toBeVisible();
    await expect(dialog.getByText(en["shell.signOut.body"])).toBeVisible();
    await expectAccessible(page, "Sign out now? dialog");
    await dialog.getByRole("button", { name: en["shell.signOut.cancel"] }).click();
    await expect(dialog).toHaveCount(0);
    await stub.release();
    await expect(answer.getByRole("heading", { name: en["chat.turn.sources"] })).toBeVisible();
    expect(idp.logoutRequests).toEqual([]);
    await expect(app.heading()).not.toHaveText(en["access.signedOut.title"]);
  });
});
