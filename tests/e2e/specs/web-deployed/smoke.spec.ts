// The deployed web smoke tests (T-348; 10-web-client.md section 13.3): unauthenticated checks of a
// deployed site after each web deploy, run by the `web-deployed` job of deployed-tests.yaml with
// the environment's public values in WEB_BASE_URL, API_BASE_URL, WEB_CLIENT_ID, API_SCOPE and
// AZURE_TENANT_ID. Any failure fails the deploy job.

import { randomUUID } from "node:crypto";

import { expect, test } from "@playwright/test";

import {
  BLOCKED_PATHS,
  NOT_FOUND_TITLE,
  globalHeaders,
  headerMismatches,
} from "../../fixtures/web/hosting.ts";

function required(name: string): string {
  const value = process.env[name];
  if (value === undefined || value === "") throw new Error(`${name} isn't set`);
  return value;
}

const site = new URL(required("WEB_BASE_URL")).origin;
const apiOrigin = new URL(required("API_BASE_URL")).origin;
const tenantId = required("AZURE_TENANT_ID");
const clientId = required("WEB_CLIENT_ID");
const apiScope = required("API_SCOPE");
const LOGIN = "https://login.microsoftonline.com";

test("T-348 every response carries the headers of sections 3.2 to 3.5 with this environment's API origin", async ({
  request,
}) => {
  const shell = await request.get(`${site}/`, { maxRedirects: 0 });
  const asset = /src="(\/assets\/[^"]+\.js)"/.exec(await shell.text())?.[1];
  expect(asset, "the shell's module script").toBeDefined();
  const served = [
    "/",
    "/index.html",
    `/chat/${randomUUID()}`,
    "/config.json",
    "/favicon.svg",
    "/not-found.html",
    asset ?? "/",
  ];
  for (const path of served) {
    const response = await request.get(`${site}${path}`, { maxRedirects: 0 });
    expect(response.status(), path).toBe(200);
    expect(headerMismatches(path, response.headers(), apiOrigin)).toEqual([]);
  }
  // A 404 carries the global headers too; which Cache-Control a missing asset gets is the
  // platform's choice between the route's and the global value.
  for (const path of ["/assets/missing.js", ...BLOCKED_PATHS]) {
    const headers = (await request.get(`${site}${path}`, { maxRedirects: 0 })).headers();
    const mismatches = Object.entries(globalHeaders(apiOrigin))
      .filter(([name, value]) => headers[name] !== value)
      .map(([name]) => `${path}: ${name} is ${String(headers[name])}`);
    expect(mismatches).toEqual([]);
  }
});

test("T-348 /chat/<random UUID> answers 200 with the app shell", async ({ request }) => {
  const response = await request.get(`${site}/chat/${randomUUID()}`, { maxRedirects: 0 });
  expect(response.status()).toBe(200);
  const html = await response.text();
  expect(html).toContain('<html lang="en">');
  expect(html).toMatch(/<script type="module"[^>]* src="\/assets\//);
});

test("T-348 the .auth routes answer 404 with no Location and no Set-Cookie", async ({
  request,
}) => {
  for (const path of BLOCKED_PATHS) {
    const response = await request.get(`${site}${path}`, { maxRedirects: 0 });
    expect(response.status(), path).toBe(404);
    expect(response.headers()["location"], path).toBeUndefined();
    expect(response.headers()["set-cookie"], path).toBeUndefined();
  }
});

test("T-348 /assets/missing.js answers 404", async ({ request }) => {
  const response = await request.get(`${site}/assets/missing.js`, { maxRedirects: 0 });
  expect(response.status()).toBe(404);
  expect(await response.text()).toContain(`<title>${NOT_FOUND_TITLE}</title>`);
});

test("T-348 /config.json holds exactly the environment's four values", async ({ request }) => {
  const response = await request.get(`${site}/config.json`, { maxRedirects: 0 });
  expect(response.status()).toBe(200);
  const config = (await response.json()) as Record<string, unknown>;
  expect(Object.keys(config)).toEqual(["tenantId", "clientId", "apiBaseUrl", "apiScope"]);
  expect(config).toEqual({
    tenantId,
    clientId,
    apiBaseUrl: required("API_BASE_URL"),
    apiScope,
  });
});

test("T-348 loading / navigates to the tenant's authorize endpoint with no CSP violation", async ({
  page,
}) => {
  const violations: string[] = [];
  await page.exposeBinding("__docchatReportCspViolation", (_, text: string) => {
    violations.push(text);
  });
  await page.addInitScript(() => {
    document.addEventListener(
      "securitypolicyviolation",
      (event) => {
        const report = (window as unknown as Record<string, ((text: string) => void) | undefined>)[
          "__docchatReportCspViolation"
        ];
        report?.(`${event.violatedDirective} refused ${event.blockedURI || "inline"}`);
      },
      true,
    );
  });
  const before: string[] = [];
  let authorizeSeen = false;
  page.on("request", (request) => {
    if (!authorizeSeen) before.push(request.url());
  });
  const authorize = new Promise<URL>((resolve) => {
    void page.route(`${LOGIN}/${tenantId}/oauth2/v2.0/authorize**`, async (route) => {
      authorizeSeen = true;
      resolve(new URL(route.request().url()));
      // The navigation is stopped here: the check needs its parameters, not a real sign-in.
      await route.fulfill({ status: 200, contentType: "text/html", body: "<!doctype html>" });
    });
  });
  await page.goto(`${site}/`);
  const url = await authorize;
  expect(url.searchParams.get("client_id")).toBe(clientId);
  expect(url.searchParams.get("redirect_uri")).toBe(`${site}/`);
  expect(url.searchParams.get("scope")?.split(" ")).toContain(apiScope);
  expect(url.searchParams.get("code_challenge_method")).toBe("S256");
  expect(url.searchParams.get("response_type")).toBe("code");
  const elsewhere = before
    .filter((target) => target.startsWith("http:") || target.startsWith("https:"))
    .filter((target) => ![site, LOGIN].includes(new URL(target).origin));
  expect(elsewhere).toEqual([]);
  expect(violations).toEqual([]);
});
