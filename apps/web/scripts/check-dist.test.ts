// @vitest-environment node
import { spawnSync } from "node:child_process";
import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";

import { describe, expect, test } from "vitest";

const SCRIPT = resolve(import.meta.dirname, "check-dist.mjs");

const CLEAN: Record<string, string | Uint8Array> = {
  "index.html": [
    "<!doctype html>",
    '<html lang="en"><head><meta charset="UTF-8" />',
    '<script type="module" crossorigin src="/assets/index-abc.js"></script>',
    '<link rel="modulepreload" crossorigin href="/assets/vendor-def.js">',
    '<link rel="stylesheet" crossorigin href="/assets/index-abc.css">',
    '</head><body><div id="root"></div></body></html>',
  ].join("\n"),
  "assets/index-abc.js":
    'import{a as b}from"./vendor-def.js";const lazy=()=>import("./Memory-123.js");b(lazy);',
  "assets/vendor-def.js": "export function a(x){return x}",
  "assets/Memory-123.js": "export const MemoryScreen=()=>null;",
  "assets/index-abc.css": ".x{color:red}",
  "favicon.svg": '<svg xmlns="http://www.w3.org/2000/svg"></svg>',
  "not-found.html": '<!doctype html><html lang="en"><body><h1>Page not found</h1></body></html>',
};

function distWith(files: Record<string, string | Uint8Array | null>): string {
  const dist = mkdtempSync(join(tmpdir(), "docchat-check-dist-"));
  for (const [name, content] of Object.entries({ ...CLEAN, ...files })) {
    if (content === null) continue;
    mkdirSync(dirname(join(dist, name)), { recursive: true });
    writeFileSync(join(dist, name), content);
  }
  return dist;
}

function check(dist: string, args: string[] = ["--dist", dist]) {
  const result = spawnSync(process.execPath, [SCRIPT, ...args], { encoding: "utf8" });
  return { status: result.status, stderr: result.stderr, stdout: result.stdout };
}

/** Incompressible bytes from a fixed seed, so the size check is repeatable. */
function noise(length: number): string {
  let state = 0x2545f491;
  let text = "";
  for (let index = 0; index < length; index += 1) {
    state ^= state << 13;
    state ^= state >>> 17;
    state ^= state << 5;
    // Letters and digits only, so the noise can't spell a forbidden string or an import.
    text += "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789".charAt(
      (state >>> 0) % 62,
    );
  }
  return `const n="${text}";`;
}

describe("the build-output gate", () => {
  test("T-343 passes on a clean build", () => {
    const result = check(distWith({}));
    expect(result.status).toBe(0);
    expect(result.stdout).toContain("check-dist: 7 files pass");
  });

  test.each([
    ["a planted .map file", { "assets/index-abc.js.map": "{}" }, "assets/index-abc.js.map"],
    ["a planted config.json", { "config.json": "{}" }, "config.json"],
    [
      "a planted staticwebapp.config.json",
      { "staticwebapp.config.json": "{}" },
      "staticwebapp.config.json",
    ],
    ["a nested config.json", { "assets/config.json": "{}" }, "assets/config.json"],
  ])("T-343 fails on %s, naming it", (_, files, name) => {
    const result = check(distWith(files));
    expect(result.status).toBe(1);
    expect(result.stderr).toContain(`check-dist: ${name}:`);
  });

  test.each([
    [
      "an inline script",
      "<html><body><script>alert(1)</script></body></html>",
      "an inline <script> without src",
    ],
    ["a <style> element", "<html><head><style></style></head></html>", "a <style> element"],
    [
      "a style= attribute",
      '<html><body><p style="color:red">x</p></body></html>',
      "a style= attribute",
    ],
  ])("T-343 fails on %s in an HTML file", (_, html, problem) => {
    const result = check(distWith({ "not-found.html": html }));
    expect(result.status).toBe(1);
    expect(result.stderr).toContain(`not-found.html: ${problem}`);
  });

  test.each([
    "AccountKey=",
    "SharedAccessSignature",
    "sig=",
    "x-functions-key",
    "-----BEGIN",
    ".blob.core.windows.net",
    ".azurewebsites.net",
    ".search.windows.net",
    ".openai.azure.com",
    "api-key",
  ])("T-343 fails on the forbidden string %s in any file", (forbidden) => {
    const result = check(distWith({ "assets/vendor-def.js": `export const v="${forbidden}";` }));
    expect(result.status).toBe(1);
    expect(result.stderr).toContain(
      `assets/vendor-def.js: contains the forbidden string "${forbidden}"`,
    );
  });

  test("T-343 fails when initial JavaScript is above 500 KB gzip-compressed", () => {
    const result = check(
      distWith({ "assets/vendor-def.js": `export function a(x){return x}${noise(700_000)}` }),
    );
    expect(result.status).toBe(1);
    expect(result.stderr).toMatch(
      /initial JavaScript is \d+ bytes gzip-compressed, above 512000: assets\/index-abc\.js, assets\/vendor-def\.js/,
    );
  });

  test("T-343 a large lazy chunk doesn't count towards the first page load", () => {
    expect(check(distWith({ "assets/Memory-123.js": noise(700_000) })).status).toBe(0);
  });

  test("a static import that is missing fails the gate", () => {
    const result = check(distWith({ "assets/vendor-def.js": null }));
    expect(result.status).toBe(1);
    expect(result.stderr).toContain("assets/vendor-def.js: missing");
  });

  test("an absolute static import is followed too", () => {
    const result = check(distWith({ "assets/index-abc.js": 'import "/assets/vendor-def.js";' }));
    expect(result.status).toBe(0);
  });

  test("a dist without index.html fails, and so does one that doesn't exist", () => {
    expect(check(distWith({ "index.html": null })).stderr).toContain("index.html: missing");
    const absent = join(mkdtempSync(join(tmpdir(), "docchat-check-dist-")), "absent");
    const result = check(absent);
    expect(result.status).toBe(1);
    expect(result.stderr).toContain("is not a readable directory");
  });

  test("wrong arguments exit 2", () => {
    expect(check("", ["--verbose"]).status).toBe(2);
  });

  test("without arguments it checks dist in the working directory", () => {
    const project = mkdtempSync(join(tmpdir(), "docchat-check-dist-"));
    for (const [name, content] of Object.entries(CLEAN)) {
      mkdirSync(dirname(join(project, "dist", name)), { recursive: true });
      writeFileSync(join(project, "dist", name), content);
    }
    const result = spawnSync(process.execPath, [SCRIPT], { cwd: project, encoding: "utf8" });
    expect(result.status).toBe(0);
  });
});
