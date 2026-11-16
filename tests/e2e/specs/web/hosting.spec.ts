// The hosting headers browser test (T-347; 10-web-client.md sections 3.2 to 3.6, acceptance
// criterion 6): the shipped build under the Static Web Apps CLI with the generated site
// configuration.

import { randomUUID } from "node:crypto";

import { BLOCKED_PATHS, NOT_FOUND_TITLE, headerMismatches } from "../../fixtures/web/hosting.ts";
import { API_ORIGIN, APP_ORIGIN } from "../../fixtures/web/stub-api/protocol.ts";
import { expect, expectAccessible, test } from "../../fixtures/web/test.ts";

test("T-347 every response of the signed-in app carries the site's headers", async ({
  app,
  page,
}) => {
  const seen: { path: string; status: number; headers: Record<string, string> }[] = [];
  page.on("response", (response) => {
    const url = new URL(response.url());
    if (url.origin === APP_ORIGIN) {
      seen.push({ path: url.pathname, status: response.status(), headers: response.headers() });
    }
  });
  await app.openShell("/chat");
  await page.goto("/memory");
  await app.waitForShell();
  const served = seen.filter((item) => item.status === 200);
  expect(served.map((item) => item.path)).toEqual(
    expect.arrayContaining(["/", "/config.json", "/memory"]),
  );
  expect(served.some((item) => item.path.startsWith("/assets/"))).toBe(true);
  expect(served.flatMap((item) => headerMismatches(item.path, item.headers, API_ORIGIN))).toEqual(
    [],
  );
});

test("T-347 the shell, the configuration and the files answer with the site's headers", async ({
  request,
}) => {
  const shell = await request.get(`/chat/${randomUUID()}`);
  expect(shell.status()).toBe(200);
  const html = await shell.text();
  expect(html).toContain('<script type="module"');
  expect(html).toContain('<html lang="en">');
  const asset = /src="(\/assets\/[^"]+\.js)"/.exec(html)?.[1];
  expect(asset).toBeDefined();

  for (const path of [
    "/",
    "/index.html",
    `/chat/${randomUUID()}`,
    "/config.json",
    "/favicon.svg",
    "/not-found.html",
    asset ?? "/assets/missing.js",
  ]) {
    const response = await request.get(path, { maxRedirects: 0 });
    expect(response.status(), path).toBe(200);
    expect(headerMismatches(path, response.headers(), API_ORIGIN)).toEqual([]);
  }
  const config = await request.get("/config.json");
  expect(config.headers()["content-type"]).toMatch(/^application\/json/);
});

test("T-347 a missing asset and the .auth routes answer 404 with the 404 page", async ({
  request,
  page,
}) => {
  // The CLI's emulator applies the global headers to answers it serves from files, not to the
  // 404 page it substitutes; the deployed smoke tests read those headers from the platform.
  for (const path of ["/assets/missing.js", "/missing.png", ...BLOCKED_PATHS]) {
    const response = await request.get(path, { maxRedirects: 0 });
    expect(response.status(), path).toBe(404);
    expect(await response.text(), path).toContain(`<title>${NOT_FOUND_TITLE}</title>`);
    expect(response.headers()["location"], path).toBeUndefined();
    expect(response.headers()["set-cookie"], path).toBeUndefined();
  }
  const posted = await request.post("/.auth/login/aad", { maxRedirects: 0 });
  expect(posted.status()).toBe(404);
  expect(posted.headers()["location"]).toBeUndefined();

  // The 404 page itself: static HTML with no script and a link back to the app.
  await page.goto("/not-found.html");
  await expect(page).toHaveTitle(NOT_FOUND_TITLE);
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Page not found");
  await expect(page.getByRole("link", { name: "go to DocChat" })).toHaveAttribute("href", "/");
  await expectAccessible(page, "the 404 page");
});
