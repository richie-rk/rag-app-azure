// The site under test (10-web-client.md section 13.2, ADR 0092): the shipped, unconfigured build,
// configured by the web client's own generator with `--allow-loopback` and the stub API's origin,
// then served by the pinned Static Web Apps CLI with `swa start <dir> --port 4280`, which applies
// the generated staticwebapp.config.json. The build is copied first, so the artifact stays
// environment-free. Run with `node fixtures/web/site.ts`; Node strips the type annotations itself.
//
// Environment: WEB_DIST (the unconfigured build; default apps/web/dist) and the generator's four
// inputs AZURE_TENANT_ID, WEB_CLIENT_ID, API_BASE_URL and API_SCOPE, which playwright.config.ts
// passes.

import { spawn, spawnSync } from "node:child_process";
import { cpSync, existsSync, readFileSync, rmSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const packageRoot = resolve(here, "../..");
const repositoryRoot = resolve(packageRoot, "../..");

const dist = resolve(process.env["WEB_DIST"] ?? join(repositoryRoot, "apps/web/dist"));
const site = join(packageRoot, "build/site");
const generator = join(repositoryRoot, "apps/web/scripts/write-env-config.mjs");

if (!existsSync(join(dist, "index.html"))) {
  process.stderr.write(`site: no web build at ${dist}; run npm run build in apps/web first\n`);
  process.exit(1);
}

rmSync(site, { recursive: true, force: true });
cpSync(dist, site, { recursive: true });

const configured = spawnSync(process.execPath, [generator, "--dist", site, "--allow-loopback"], {
  stdio: "inherit",
});
if (configured.status !== 0) process.exit(configured.status ?? 1);

const require = createRequire(import.meta.url);
const manifestPath = require.resolve("@azure/static-web-apps-cli/package.json");
const manifest = JSON.parse(readFileSync(manifestPath, "utf8")) as { bin: { swa: string } };
const cli = join(dirname(manifestPath), manifest.bin.swa);

const server = spawn(
  process.execPath,
  [cli, "start", site, "--port", "4280", "--host", "127.0.0.1"],
  { stdio: "inherit" },
);
for (const signal of ["SIGINT", "SIGTERM"] as const) {
  process.on(signal, () => {
    server.kill(signal);
  });
}
server.on("exit", (code, signal) => {
  process.exit(code ?? (signal === null ? 1 : 0));
});
