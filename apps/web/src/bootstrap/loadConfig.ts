// The runtime configuration (10-web-client.md section 2.1): `/config.json`, exactly four public
// values written per environment at deploy time. Nothing else configures the client.

import { createContext, useContext } from "react";
import { z } from "zod";

import "./zodSetup";
import { fetchConfigDocument } from "../api/http";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const SCOPE = /^api:\/\/[^\s/][^\s]*\/access_as_user$/;
const LOOPBACK_HOSTS: ReadonlySet<string> = new Set(["localhost", "127.0.0.1"]);

export interface AppConfig {
  readonly tenantId: string;
  readonly clientId: string;
  /** The API's origin. */
  readonly apiBaseUrl: string;
  /** The one scope every token request names. */
  readonly apiScope: string;
}

/** The API origin rule: `https:` with a lower-case host and optional port and nothing after it;
 * the loopback `http:` form only when the page itself is served from that loopback host. */
export function isAllowedApiOrigin(value: string, pageHostname: string): boolean {
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    return false;
  }
  if (url.origin !== value) return false;
  if (url.protocol === "https:") return true;
  return (
    url.protocol === "http:" && LOOPBACK_HOSTS.has(url.hostname) && url.hostname === pageHostname
  );
}

/** The strict schema of `config.json`: unknown members are rejected. */
export function configSchema(pageHostname: string) {
  return z.strictObject({
    tenantId: z.string().regex(UUID),
    clientId: z.string().regex(UUID),
    apiBaseUrl: z.string().refine((value) => isAllowedApiOrigin(value, pageHostname)),
    apiScope: z.string().max(255).regex(SCOPE),
  });
}

/** The client outcome `config_unavailable`: the Configuration error screen follows. */
export class ConfigUnavailable extends Error {
  constructor() {
    super("config_unavailable");
    this.name = "ConfigUnavailable";
  }
}

/** Parses a fetched `config.json`, or throws `ConfigUnavailable`. */
export function parseConfig(value: unknown, pageHostname: string): AppConfig {
  const parsed = configSchema(pageHostname).safeParse(value);
  if (!parsed.success) throw new ConfigUnavailable();
  return parsed.data;
}

/** Fetches and validates `/config.json` (section 4.2 step 3). A network error, a status other
 * than `200`, invalid JSON or any rule failure throws `ConfigUnavailable`. */
export async function loadConfig(
  pageHostname: string = window.location.hostname,
): Promise<AppConfig> {
  let value: unknown;
  try {
    value = await fetchConfigDocument();
  } catch {
    throw new ConfigUnavailable();
  }
  return parseConfig(value, pageHostname);
}

export const ConfigContext = createContext<AppConfig | null>(null);

/** The validated configuration; only inside the signed-in app. */
export function useConfig(): AppConfig {
  const config = useContext(ConfigContext);
  if (config === null) throw new Error("useConfig outside the signed-in app");
  return config;
}
