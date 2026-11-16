import { defineConfig, devices, type Project } from "@playwright/test";

import { API_SCOPE, CLIENT_ID, TENANT_ID } from "./fixtures/web/identity.ts";
import { API_ORIGIN, APP_ORIGIN, CONTROL_ORIGIN } from "./fixtures/web/stub-api/protocol.ts";

// The browser suites (84-testing.md section 2.13, 10-web-client.md section 13). `specs/web/` runs
// on the four projects against the shipped build under the Static Web Apps CLI and the stub API;
// `specs/web-deployed/` runs on `chromium` alone against a deployed site, which the `web-deployed`
// job names with WEB_BASE_URL and the environment's public values, so no local server starts.

const ci = process.env["CI"] !== undefined && process.env["CI"] !== "";
const deployed = process.env["WEB_BASE_URL"] !== undefined;
// Local runs only: a preinstalled Chromium other than the build this Playwright release
// downloads. CI installs the matching browsers and never sets it.
const localChromium = process.env["E2E_CHROMIUM_EXECUTABLE"];

// Chromium recognises the Permissions-Policy feature `web-share` only where it ships Web Share
// (Windows, macOS, ChromeOS, Android); on the Linux runners it warns "Unrecognized feature", which
// the gates of every test refuse. Enabling the feature makes the Linux build read the header as
// those platforms do, and any other unrecognised feature still fails.
const CHROMIUM_ARGS = ["--enable-blink-features=WebShare"];

const chromium: Project["use"] = {
  ...devices["Desktop Chrome"],
  launchOptions: {
    args: CHROMIUM_ARGS,
    ...(localChromium === undefined ? {} : { executablePath: localChromium }),
  },
};

const webProjects: Project[] = [
  { name: "chromium", testMatch: "web/**/*.spec.ts", use: chromium },
  { name: "firefox", testMatch: "web/**/*.spec.ts", use: { ...devices["Desktop Firefox"] } },
  { name: "webkit", testMatch: "web/**/*.spec.ts", use: { ...devices["Desktop Safari"] } },
  {
    name: "msedge",
    testMatch: "web/**/*.spec.ts",
    // Edge's own PDF viewer: the citation viewer's frame path (T-387).
    use: { ...devices["Desktop Edge"], channel: "msedge", launchOptions: { args: CHROMIUM_ARGS } },
  },
];

const deployedProjects: Project[] = [
  { name: "chromium", testMatch: "web-deployed/**/*.spec.ts", use: chromium },
];

export default defineConfig({
  testDir: "specs",
  forbidOnly: ci,
  retries: ci ? 1 : 0,
  reporter: ci ? [["list"], ["github"]] : [["list"]],
  outputDir: "test-results",
  timeout: 60_000,
  use: {
    trace: "retain-on-failure",
    ...(deployed ? {} : { baseURL: APP_ORIGIN }),
  },
  projects: deployed ? deployedProjects : webProjects,
  ...(deployed
    ? {}
    : {
        webServer: [
          {
            command: "node fixtures/web/stub-api/server.ts",
            url: `${CONTROL_ORIGIN}/health`,
            reuseExistingServer: !ci,
            timeout: 30_000,
          },
          {
            command: "node fixtures/web/site.ts",
            url: `${APP_ORIGIN}/`,
            reuseExistingServer: !ci,
            timeout: 120_000,
            env: {
              AZURE_TENANT_ID: TENANT_ID,
              WEB_CLIENT_ID: CLIENT_ID,
              API_BASE_URL: API_ORIGIN,
              API_SCOPE,
            },
          },
        ],
      }),
});
