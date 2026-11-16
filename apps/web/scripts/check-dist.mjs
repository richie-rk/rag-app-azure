#!/usr/bin/env node
// The build-output gate (10-web-client.md section 1.7): the build that reaches every environment
// holds no configuration file, source map, inline code, endpoint, key or environment value, and
// its first page load stays small. Node.js standard library only.

import { readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join, relative, resolve, sep } from "node:path";
import { constants, gzipSync } from "node:zlib";

const FORBIDDEN_FILES = new Set(["config.json", "staticwebapp.config.json"]);
const FORBIDDEN_STRINGS = [
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
];
const INITIAL_JS_LIMIT = 500 * 1024;

const SCRIPT_TAG = /<script\b([^>]*)>/gi;
const STYLE_ELEMENT = /<style\b/i;
const STYLE_ATTRIBUTE = /<[^>]*\sstyle\s*=/i;
const SRC_ATTRIBUTE = /\ssrc\s*=/i;
const ENTRY_SCRIPT =
  /<script\b[^>]*\btype\s*=\s*["']?module["']?[^>]*\bsrc\s*=\s*["']([^"']+)["'][^>]*>/gi;
// Static `import … from "x"`, `import "x"` and `export … from "x"`; a dynamic `import(` is lazy.
const STATIC_IMPORT =
  /(?:^|[^\w$.])(?:import|export)\s*(?:[\w$*{}\s,]*?\s*from\s*)?["']([^"']+)["']/g;

function parseArguments(argv) {
  if (argv.length === 0) return "dist";
  if (argv.length === 2 && argv[0] === "--dist") return argv[1];
  process.stderr.write("usage: check-dist.mjs [--dist <dir>]\n");
  process.exit(2);
}

function listFiles(root) {
  const files = [];
  for (const name of readdirSync(root)) {
    const path = join(root, name);
    if (statSync(path).isDirectory()) {
      files.push(...listFiles(path));
    } else {
      files.push(path);
    }
  }
  return files;
}

function inlineCodeProblems(html) {
  const problems = [];
  for (const match of html.matchAll(SCRIPT_TAG)) {
    if (!SRC_ATTRIBUTE.test(match[1])) problems.push("an inline <script> without src");
  }
  if (STYLE_ELEMENT.test(html)) problems.push("a <style> element");
  if (STYLE_ATTRIBUTE.test(html)) problems.push("a style= attribute");
  return problems;
}

/** The entry chunks of `index.html` and every chunk they import statically, as absolute paths. */
function initialChunks(dist) {
  const indexPath = join(dist, "index.html");
  let html;
  try {
    html = readFileSync(indexPath, "utf8");
  } catch {
    return { chunks: [], missing: ["index.html"] };
  }
  const pending = [...html.matchAll(ENTRY_SCRIPT)].map((match) =>
    join(dist, match[1].replace(/^\//, "")),
  );
  const chunks = new Set();
  const missing = [];
  while (pending.length > 0) {
    const path = pending.pop();
    if (chunks.has(path)) continue;
    let code;
    try {
      code = readFileSync(path, "utf8");
    } catch {
      missing.push(relative(dist, path));
      continue;
    }
    chunks.add(path);
    for (const match of code.matchAll(STATIC_IMPORT)) {
      const specifier = match[1];
      if (specifier.startsWith(".")) pending.push(resolve(dirname(path), specifier));
      else if (specifier.startsWith("/")) pending.push(join(dist, specifier.slice(1)));
    }
  }
  return { chunks: [...chunks], missing };
}

function main() {
  const dist = resolve(parseArguments(process.argv.slice(2)));
  const failures = [];
  let files;
  try {
    files = listFiles(dist);
  } catch {
    process.stderr.write(`check-dist: ${dist} is not a readable directory\n`);
    process.exit(1);
  }

  for (const path of files) {
    const name = relative(dist, path).split(sep).join("/");
    const base = name.split("/").pop();
    if (FORBIDDEN_FILES.has(base))
      failures.push(`${name}: configuration files are written at deploy time only`);
    if (base.endsWith(".map")) failures.push(`${name}: source maps never ship`);
    const text = readFileSync(path, "latin1");
    if (base.endsWith(".html")) {
      for (const problem of inlineCodeProblems(text)) failures.push(`${name}: ${problem}`);
    }
    for (const forbidden of FORBIDDEN_STRINGS) {
      if (text.includes(forbidden))
        failures.push(`${name}: contains the forbidden string "${forbidden}"`);
    }
  }

  const { chunks, missing } = initialChunks(dist);
  for (const name of missing) failures.push(`${name}: missing, but the first page load needs it`);
  let initialBytes = 0;
  for (const path of chunks) {
    initialBytes += gzipSync(readFileSync(path), { level: constants.Z_BEST_COMPRESSION }).length;
  }
  if (initialBytes > INITIAL_JS_LIMIT) {
    const names = chunks
      .map((path) => relative(dist, path).split(sep).join("/"))
      .sort()
      .join(", ");
    failures.push(
      `initial JavaScript is ${initialBytes} bytes gzip-compressed, above ${INITIAL_JS_LIMIT}: ${names}`,
    );
  }

  if (failures.length > 0) {
    for (const failure of failures) process.stderr.write(`check-dist: ${failure}\n`);
    process.exit(1);
  }
  process.stdout.write(
    `check-dist: ${files.length} files pass; initial JavaScript ${initialBytes} bytes gzip-compressed\n`,
  );
}

main();
