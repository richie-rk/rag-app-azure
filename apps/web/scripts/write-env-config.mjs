#!/usr/bin/env node
// Writes <dist>/config.json and <dist>/staticwebapp.config.json for one environment
// (10-web-client.md section 2.2). It is the only way either file is produced, so the build that
// reaches every environment holds no environment value. Node.js standard library only.

import { existsSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { join } from "node:path";

const TEMPLATE = new URL("../staticwebapp.config.template.json", import.meta.url);
const PLACEHOLDER = "__API_ORIGIN__";
const SITE_CONFIG_LIMIT = 20_480;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const SCOPE = /^api:\/\/[^\s/][^\s]*\/access_as_user$/;
const LOOPBACK_HOSTS = new Set(["localhost", "127.0.0.1"]);

class Refusal extends Error {}

function refuse(message) {
  throw new Refusal(message);
}

function parseArguments(argv) {
  let dist = "dist";
  let allowLoopback = false;
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    if (argument === "--allow-loopback") {
      allowLoopback = true;
    } else if (argument === "--dist" && index + 1 < argv.length) {
      dist = argv[index + 1];
      index += 1;
    } else {
      refuse(
        `unknown argument ${argument}; usage: write-env-config.mjs [--dist <dir>] [--allow-loopback]`,
      );
    }
  }
  return { dist, allowLoopback };
}

function required(environment, name) {
  const value = environment[name];
  if (value === undefined || value === "") {
    refuse(`${name} is not set`);
  }
  return value;
}

/** The API origin rule of section 2.1: `https:` with a lower-case host and optional port, and
 * nothing after it; the loopback `http:` form only when the browser-test harness asks for it. */
function checkOrigin(value, allowLoopback) {
  let url;
  try {
    url = new URL(value);
  } catch {
    refuse("API_BASE_URL is not a URL");
  }
  if (url.origin !== value) {
    refuse(
      "API_BASE_URL must be an origin: lower-case host, no path, query, fragment or trailing slash",
    );
  }
  if (url.protocol === "https:") {
    return url.origin;
  }
  if (url.protocol === "http:" && LOOPBACK_HOSTS.has(url.hostname)) {
    if (!allowLoopback) {
      refuse("API_BASE_URL is a loopback origin, which only --allow-loopback permits");
    }
    return url.origin;
  }
  refuse("API_BASE_URL must use https:");
}

function buildFiles(environment, allowLoopback) {
  const tenantId = required(environment, "AZURE_TENANT_ID");
  if (!UUID.test(tenantId)) refuse("AZURE_TENANT_ID must be a canonical lower-case UUID");
  const clientId = required(environment, "WEB_CLIENT_ID");
  if (!UUID.test(clientId)) refuse("WEB_CLIENT_ID must be a canonical lower-case UUID");
  const apiBaseUrl = required(environment, "API_BASE_URL");
  const origin = checkOrigin(apiBaseUrl, allowLoopback);
  const apiScope = required(environment, "API_SCOPE");
  if (apiScope.length > 255 || !SCOPE.test(apiScope)) {
    refuse("API_SCOPE must be an api:// URI ending in /access_as_user, at most 255 characters");
  }

  const config = `${JSON.stringify({ tenantId, clientId, apiBaseUrl, apiScope })}\n`;

  const template = readFileSync(TEMPLATE, "utf8");
  if (template.split(PLACEHOLDER).length !== 2) {
    refuse(`the site configuration template must hold ${PLACEHOLDER} exactly once`);
  }
  let site;
  try {
    site = JSON.parse(template.replace(PLACEHOLDER, origin));
  } catch {
    refuse("the site configuration template is not valid JSON");
  }
  const siteText = `${JSON.stringify(site, null, 2)}\n`;
  if (Buffer.byteLength(siteText, "utf8") > SITE_CONFIG_LIMIT) {
    refuse(`staticwebapp.config.json would exceed ${SITE_CONFIG_LIMIT} bytes`);
  }
  return { config, siteText };
}

function main() {
  try {
    const { dist, allowLoopback } = parseArguments(process.argv.slice(2));
    if (!existsSync(dist) || !statSync(dist).isDirectory()) {
      refuse(`--dist ${dist} is not a directory`);
    }
    // Both files are built and checked before either is written, so a refusal writes nothing.
    const { config, siteText } = buildFiles(process.env, allowLoopback);
    writeFileSync(join(dist, "config.json"), config, "utf8");
    writeFileSync(join(dist, "staticwebapp.config.json"), siteText, "utf8");
  } catch (error) {
    if (error instanceof Refusal) {
      process.stderr.write(`write-env-config: ${error.message}\n`);
      process.exit(1);
    }
    throw error;
  }
}

main();
