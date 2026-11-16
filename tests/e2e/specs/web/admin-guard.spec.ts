// The admin guard browser test (T-352; 10-web-client.md sections 4.7 and 6.2, acceptance criterion
// 11): Admin only for a non-admin, with no admin call and no admin module downloaded, and Admin
// only after an admin call answers `403 admin_required`.

import { readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { adminUser } from "../../fixtures/web/stub-api/builders.ts";
import { APP_ORIGIN } from "../../fixtures/web/stub-api/protocol.ts";
import { en, expect, expectAccessible, test } from "../../fixtures/web/test.ts";

/** The modules of `src/admin/`, by file name: Vite names a chunk after the module it starts at. */
function adminModuleNames(): Set<string> {
  const root = fileURLToPath(new URL("../../../../apps/web/src/admin", import.meta.url));
  return new Set(
    readdirSync(root, { recursive: true, withFileTypes: true })
      .filter((entry) => entry.isFile() && /^[^.]+\.tsx?$/.test(entry.name))
      .map((entry) => entry.name.replace(/\.tsx?$/, "")),
  );
}

/** The chunk names of the scripts a page requested: `/assets/<name>-<8-character hash>.js`. */
function chunkNames(requests: readonly string[]): Set<string> {
  const prefix = `GET ${APP_ORIGIN}/assets/`;
  return new Set(
    requests
      .filter((request) => request.startsWith(prefix) && request.endsWith(".js"))
      .map((request) => request.slice(prefix.length).replace(/-[A-Za-z0-9_-]{8}\.js$/, "")),
  );
}

test("T-352 a non-admin sees Admin only, makes no admin call and downloads no admin module", async ({
  app,
  page,
  stub,
  gates,
}) => {
  await app.openShell("/admin/projects");
  await expect(app.heading()).toHaveText(en["access.adminOnly.title"]);
  await expect(page.getByRole("button", { name: en["access.adminOnly.action"] })).toBeVisible();
  await expect(page.getByRole("navigation").getByText(en["shell.nav.admin"])).toHaveCount(0);
  await expectAccessible(page, "Admin only");
  const asUser = chunkNames(gates.requests);
  const admin = adminModuleNames();
  expect([...asUser].filter((name) => admin.has(name))).toEqual([]);
  const calls = await stub.requests();
  expect(calls.filter((call) => call.operation?.startsWith("admin") === true)).toEqual([]);

  // The same route as an administrator downloads modules the user's page never requested.
  await stub.update({ user: adminUser() });
  const before = gates.requests.length;
  await page.reload();
  await expect(app.heading()).toHaveText(en["projects.title"]);
  const asAdmin = chunkNames(gates.requests.slice(before));
  const adminOnly = [...asAdmin].filter((name) => !asUser.has(name));
  expect(adminOnly.length).toBeGreaterThan(0);
  expect(adminOnly.some((name) => admin.has(name))).toBe(true);
});

test.describe(() => {
  test.use({ world: { user: adminUser() } });

  test("T-352 an admin call answered 403 admin_required refetches the user and shows Admin only", async ({
    app,
    page,
    stub,
  }) => {
    await app.openShell("/admin/projects");
    await expect(app.heading()).toHaveText(en["projects.title"]);
    await expectAccessible(page, "Projects");
    // The role is removed during the session; the next admin call is refused.
    await stub.update({ user: { isAdmin: false, roles: ["User"] } });
    await page.getByRole("searchbox", { name: en["projects.filter"] }).fill("hand");
    await expect(app.heading()).toHaveText(en["access.adminOnly.title"]);
    const calls = await stub.requests();
    expect(calls.filter((call) => call.operation === "getCurrentUser").length).toBeGreaterThan(1);
    const refused = calls.filter(
      (call) => call.operation === "adminListProjects" && call.query["q"] === "hand",
    );
    expect(refused.length).toBeGreaterThan(0);
  });
});
