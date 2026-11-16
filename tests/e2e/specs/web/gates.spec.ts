// The shared gates fixture (T-408; 10-web-client.md sections 11.6 and 13.2, acceptance criterion
// 56): the gates refuse what they must, and axe finds nothing on every screen in both themes. The
// other specs scan the states they reach; this one walks the screens no other spec opens.

import { IDS, adminUser, session, turn } from "../../fixtures/web/stub-api/builders.ts";
import { en, expect, expectAccessible, test, type App } from "../../fixtures/web/test.ts";

/** Opens an app path and waits until its `h1` and data have arrived (no skeleton left). */
async function visit(app: App, path: string, heading: string | RegExp): Promise<void> {
  await app.page.goto(path);
  await app.waitForShell();
  await expect(app.heading()).toContainText(heading);
  await expect(app.page.locator(".fui-Skeleton")).toHaveCount(0);
}

test("T-408 the gates record a CSP violation, a console error and an uncaught error", async ({
  app,
  page,
  gates,
}) => {
  await app.openShell("/chat");
  await page.evaluate(() => {
    const script = document.createElement("script");
    script.textContent = "window.provoked = true;";
    document.body.append(script);
  });
  await expect
    .poll(() =>
      gates.cspViolations.filter((line) => /^script-src(-elem)? refused inline/.test(line)),
    )
    .toHaveLength(1);
  expect(await page.evaluate(() => "provoked" in window)).toBe(false);

  await page.evaluate(() => {
    // eslint-disable-next-line no-console
    console.error("provoked console error");
  });
  await expect.poll(() => gates.consoleProblems).toContain("error: provoked console error");
  // The browser reports the refused inline script on the console too.
  expect(gates.consoleProblems.some((line) => line.includes("Content Security Policy"))).toBe(true);

  await page.evaluate(() => {
    setTimeout(() => {
      throw new Error("provoked page error");
    });
  });
  await expect.poll(() => gates.pageErrors).toEqual(["Error: provoked page error"]);

  // Each was caught; the test removes exactly what it provoked, so anything else still fails it.
  const drop = (lines: string[], provoked: RegExp) => {
    const kept = lines.filter((line) => !provoked.test(line));
    lines.splice(0, lines.length, ...kept);
  };
  drop(gates.cspViolations, /^script-src(-elem)? refused inline/);
  drop(gates.consoleProblems, /provoked console error|Executing inline script violates/);
  drop(gates.pageErrors, /provoked page error/);
});

test("T-408 the Configuration error screen has no axe violation", async ({ app, page }) => {
  await page.route("**/config.json", (route) => route.fulfill({ status: 500, body: "" }));
  await app.open("/chat");
  await expect(app.heading()).toHaveText(en["app.configError.title"]);
  await expect(page.getByRole("button", { name: en["app.reload"] })).toBeVisible();
  await expectAccessible(page, "Configuration error");
});

test("T-408 the Unsupported browser screen makes no network call and has no axe violation", async ({
  app,
  page,
  stub,
  gates,
}) => {
  await page.addInitScript(() => {
    Reflect.deleteProperty(window, "BroadcastChannel");
  });
  await app.open("/chat");
  await expect(app.heading()).toHaveText(en["access.unsupported.title"]);
  await expectAccessible(page, "Unsupported browser");
  expect(gates.requests.filter((request) => request.includes("/config.json"))).toEqual([]);
  expect(await stub.requests()).toEqual([]);
});

test.describe(() => {
  test.use({ world: { projects: [] } });

  test("T-408 the No projects state has no axe violation", async ({ app, page, stub }) => {
    await app.openShell("/chat");
    await expect(page.getByRole("heading", { name: en["access.noProjects.title"] })).toBeVisible();
    await expectAccessible(page, "No projects");
    const before = (await stub.requests("listMyProjects")).length;
    await page.getByRole("button", { name: en["access.noProjects.action"] }).click();
    await expect.poll(async () => (await stub.requests("listMyProjects")).length).toBe(before + 1);
  });
});

test.describe(() => {
  test.use({ world: { projects: [], user: adminUser() } });

  test("T-408 the admin's No projects state has no axe violation", async ({ app, page }) => {
    await app.openShell("/chat");
    await expect(
      page.getByRole("heading", { name: en["access.noProjectsAdmin.title"] }),
    ).toBeVisible();
    await expectAccessible(page, "No projects for an admin");
    await page.getByRole("button", { name: en["access.noProjectsAdmin.action"] }).click();
    await expect(app.heading()).toHaveText(en["projects.title"]);
  });
});

test.describe(() => {
  test.use({ world: { user: adminUser() } });

  test("T-408 every admin screen has no axe violation in either theme", async ({ app, page }) => {
    const index = IDS.handbookIndex;
    const screens: readonly (readonly [string, string | RegExp])[] = [
      ["/admin/projects", en["projects.title"]],
      ["/admin/projects/new", en["project.new.title"]],
      [`/admin/projects/${IDS.handbook}?tab=details`, "Employee handbook"],
      [`/admin/projects/${IDS.handbook}?tab=members`, "Employee handbook"],
      [`/admin/projects/${IDS.handbook}?tab=indexes`, "Employee handbook"],
      ["/admin/indexes", en["indexes.title"]],
      ["/admin/indexes/new", en["index.new.title"]],
      [`/admin/indexes/${index}`, "Handbook index"],
      [`/admin/indexes/${index}/documents`, /Documents/],
      ["/admin/runs", en["runs.title"]],
      [`/admin/runs/${IDS.run}`, en["run.title"]],
    ];
    for (const [path, heading] of screens) {
      await visit(app, path, heading);
      await expectAccessible(page, path);
    }
  });
});

const STORED = session({
  title: "Leave",
  turns: [turn({ question: "How much leave?" }), turn({ question: "And carry-over?" })],
});

test.describe(() => {
  test.use({ world: { sessions: [STORED] } });

  test("T-408 the chat at the medium and narrow widths has no axe violation", async ({
    app,
    page,
  }) => {
    for (const width of [1000, 600]) {
      await page.setViewportSize({ width, height: 800 });
      await visit(app, `/chat/${STORED.sessionId}`, /.*/);
      await expect(app.turn("And carry-over?")).toBeVisible();
      await expectAccessible(page, `a session at ${String(width)} px`);
      await page.getByRole("button", { name: en["shell.nav.show"] }).click();
      await expect(page.getByRole("navigation", { name: en["shell.nav.label"] })).toBeVisible();
      await expectAccessible(page, `the navigation drawer at ${String(width)} px`);
      await page.keyboard.press("Escape");
    }
    await app.page.getByRole("button", { name: en["chat.sessions.open"] }).click();
    await expect(app.sessionPanel()).toBeVisible();
    await expectAccessible(page, "the session panel overlay at 600 px");
  });
});
