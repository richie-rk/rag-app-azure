// Files of the repository the tests read: the shared stream fixtures, the OpenAPI document and the
// design documents. Tests run in Node, so they read them from disk.

import { readFileSync, readdirSync } from "node:fs";
import { join, resolve } from "node:path";

/** The repository's root. Vitest runs in `apps/web`, its configuration's directory; module URLs
 * aren't file URLs under jsdom, so the working directory is the anchor. */
export const REPOSITORY_ROOT = resolve(process.cwd(), "..", "..");

export function repositoryPath(relative: string): string {
  return join(REPOSITORY_ROOT, relative);
}

export function readRepositoryText(relative: string): string {
  return readFileSync(repositoryPath(relative), "utf8");
}

export function readRepositoryBytes(relative: string): Uint8Array {
  return new Uint8Array(readFileSync(repositoryPath(relative)));
}

/** The shared NDJSON stream fixtures (`84-testing.md` section 1.5), by file name. */
export function streamFixtures(): [string, Uint8Array][] {
  const directory = "tests/fixtures/streams";
  return readdirSync(repositoryPath(directory))
    .filter((name) => name.endsWith(".ndjson"))
    .sort()
    .map((name) => [name, readRepositoryBytes(`${directory}/${name}`)]);
}
