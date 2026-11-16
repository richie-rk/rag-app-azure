// @vitest-environment node
import { spawnSync } from "node:child_process";
import {
  copyFileSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

import { describe, expect, test } from "vitest";

const WEB = resolve(import.meta.dirname, "..");
const SCRIPT = join(WEB, "scripts", "write-env-config.mjs");
const TEMPLATE = readFileSync(join(WEB, "staticwebapp.config.template.json"), "utf8");

const VALID = {
  AZURE_TENANT_ID: "11111111-2222-4333-8444-555555555555",
  WEB_CLIENT_ID: "66666666-7777-4888-9999-aaaaaaaaaaaa",
  API_BASE_URL: "https://app-docchat-api-dev-a1b2c3.azurewebsites.net",
  API_SCOPE: "api://11111111-2222-4333-8444-555555555555/docchat-dev-a1b2c3-api/access_as_user",
};

function freshDist(): string {
  return mkdtempSync(join(tmpdir(), "docchat-dist-"));
}

/** Runs the generator from `cwd` with only these environment variables, as the deploy does. */
function generate(variables: Record<string, string>, args: string[], script = SCRIPT, cwd = WEB) {
  const result = spawnSync(process.execPath, [script, ...args], {
    cwd,
    env: { PATH: process.env["PATH"] ?? "", ...variables },
    encoding: "utf8",
  });
  return { status: result.status, stderr: result.stderr };
}

describe("the deploy-time generator", () => {
  test("T-342 writes config.json byte for byte and the site configuration from the template", () => {
    const dist = freshDist();
    expect(generate(VALID, ["--dist", dist]).status).toBe(0);
    expect(readFileSync(join(dist, "config.json"), "utf8")).toBe(
      `{"tenantId":"${VALID.AZURE_TENANT_ID}","clientId":"${VALID.WEB_CLIENT_ID}","apiBaseUrl":"${VALID.API_BASE_URL}","apiScope":"${VALID.API_SCOPE}"}\n`,
    );
    const site = readFileSync(join(dist, "staticwebapp.config.json"), "utf8");
    expect(JSON.parse(site)).toEqual(
      JSON.parse(TEMPLATE.replace("__API_ORIGIN__", VALID.API_BASE_URL)),
    );
    expect(site).toBe(`${JSON.stringify(JSON.parse(site), null, 2)}\n`);
    expect(site).toContain(
      `connect-src 'self' ${VALID.API_BASE_URL} https://login.microsoftonline.com`,
    );
    expect(site).not.toContain("__API_ORIGIN__");
  });

  test("T-342 finds its template from the repository root too, and is idempotent", () => {
    const dist = freshDist();
    const repositoryRoot = resolve(WEB, "..", "..");
    const script = join("apps", "web", "scripts", "write-env-config.mjs");
    expect(generate(VALID, ["--dist", dist], script, repositoryRoot).status).toBe(0);
    const first = readFileSync(join(dist, "staticwebapp.config.json"));
    expect(generate(VALID, ["--dist", dist], script, repositoryRoot).status).toBe(0);
    expect(readFileSync(join(dist, "staticwebapp.config.json"))).toEqual(first);
    expect(readdirSync(dist).sort()).toEqual(["config.json", "staticwebapp.config.json"]);
  });

  test.each([
    ["AZURE_TENANT_ID", { AZURE_TENANT_ID: "" }],
    ["AZURE_TENANT_ID", { AZURE_TENANT_ID: "ABCDEF11-2222-4333-8444-555555555555" }],
    ["WEB_CLIENT_ID", { WEB_CLIENT_ID: "not-a-uuid" }],
    ["API_BASE_URL", { API_BASE_URL: "http://api.example" }],
    ["API_BASE_URL", { API_BASE_URL: "https://api.example/" }],
    ["API_BASE_URL", { API_BASE_URL: "https://api.example/v1" }],
    ["API_BASE_URL", { API_BASE_URL: "https://API.example" }],
    ["API_BASE_URL", { API_BASE_URL: "ftp://api.example" }],
    ["API_BASE_URL", { API_BASE_URL: "nonsense" }],
    ["API_SCOPE", { API_SCOPE: "api://x/docchat-api/user_impersonation" }],
    ["API_SCOPE", { API_SCOPE: `api://${"a".repeat(240)}/access_as_user` }],
  ])("T-342 an invalid %s exits 1 naming it and writes nothing", (name, change) => {
    const dist = freshDist();
    const result = generate({ ...VALID, ...change }, ["--dist", dist]);
    expect(result.status).toBe(1);
    expect(result.stderr.trim().split("\n")).toHaveLength(1);
    expect(result.stderr).toContain(name);
    expect(readdirSync(dist)).toEqual([]);
  });

  test("T-342 a missing variable exits 1 naming it", () => {
    const missing = Object.fromEntries(
      Object.entries(VALID).filter(([name]) => name !== "API_SCOPE"),
    );
    const dist = freshDist();
    const result = generate(missing, ["--dist", dist]);
    expect(result.status).toBe(1);
    expect(result.stderr).toContain("API_SCOPE");
    expect(readdirSync(dist)).toEqual([]);
  });

  test("T-342 a loopback origin needs --allow-loopback", () => {
    const dist = freshDist();
    const loopback = { ...VALID, API_BASE_URL: "http://127.0.0.1:4010" };
    const refused = generate(loopback, ["--dist", dist]);
    expect(refused.status).toBe(1);
    expect(refused.stderr).toContain("API_BASE_URL");
    expect(readdirSync(dist)).toEqual([]);
    expect(generate(loopback, ["--dist", dist, "--allow-loopback"]).status).toBe(0);
    expect(readFileSync(join(dist, "staticwebapp.config.json"), "utf8")).toContain(
      "connect-src 'self' http://127.0.0.1:4010 ",
    );
  });

  test("T-342 a site configuration above 20,480 bytes exits 1 and writes nothing", () => {
    const copy = mkdtempSync(join(tmpdir(), "docchat-generator-"));
    mkdirSync(join(copy, "scripts"));
    copyFileSync(SCRIPT, join(copy, "scripts", "write-env-config.mjs"));
    const large = JSON.parse(TEMPLATE) as { routes: unknown[] };
    for (let index = 0; index < 400; index += 1)
      large.routes.push({ route: `/legacy/${String(index)}`, statusCode: 404 });
    writeFileSync(join(copy, "staticwebapp.config.template.json"), JSON.stringify(large));
    const dist = freshDist();
    const result = generate(VALID, ["--dist", dist], join(copy, "scripts", "write-env-config.mjs"));
    expect(result.status).toBe(1);
    expect(result.stderr).toContain("20480");
    expect(readdirSync(dist)).toEqual([]);
  });

  test.each([
    ["a template without the placeholder", "{}"],
    ["a template with the placeholder twice", '{"a":"__API_ORIGIN__","b":"__API_ORIGIN__"}'],
    ["a template that isn't JSON once filled", '{"a":__API_ORIGIN__}'],
  ])("%s exits 1", (_, template) => {
    const copy = mkdtempSync(join(tmpdir(), "docchat-generator-"));
    mkdirSync(join(copy, "scripts"));
    copyFileSync(SCRIPT, join(copy, "scripts", "write-env-config.mjs"));
    writeFileSync(join(copy, "staticwebapp.config.template.json"), template);
    const dist = freshDist();
    expect(
      generate(VALID, ["--dist", dist], join(copy, "scripts", "write-env-config.mjs")).status,
    ).toBe(1);
    expect(readdirSync(dist)).toEqual([]);
  });

  test("a --dist that isn't a directory, or an unknown argument, exits 1", () => {
    const missing = join(freshDist(), "absent");
    expect(generate(VALID, ["--dist", missing]).status).toBe(1);
    expect(existsSync(missing)).toBe(false);
    const unknown = generate(VALID, ["--dist", freshDist(), "--verbose"]);
    expect(unknown.status).toBe(1);
    expect(unknown.stderr).toContain("--verbose");
  });

  test("without --dist it writes to dist in the working directory", () => {
    const cwd = freshDist();
    mkdirSync(join(cwd, "dist"));
    expect(generate(VALID, [], SCRIPT, cwd).status).toBe(0);
    expect(existsSync(join(cwd, "dist", "config.json"))).toBe(true);
  });
});
