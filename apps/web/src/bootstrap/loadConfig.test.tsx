import { renderHook } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import type { ReactNode } from "react";
import { describe, expect, test } from "vitest";

import { TEST_CONFIG } from "../test/handlers";
import { server } from "../test/server";
import {
  ConfigContext,
  ConfigUnavailable,
  isAllowedApiOrigin,
  loadConfig,
  parseConfig,
  useConfig,
} from "./loadConfig";

const VALID = {
  tenantId: "11111111-2222-4333-8444-555555555555",
  clientId: "66666666-7777-4888-9999-aaaaaaaaaaaa",
  apiBaseUrl: "https://app-docchat-api-prod-a1b2c3.azurewebsites.net",
  apiScope: "api://11111111-2222-4333-8444-555555555555/docchat-prod-a1b2c3-api/access_as_user",
};

function rejects(value: unknown, hostname = "docchat.example"): boolean {
  try {
    parseConfig(value, hostname);
    return false;
  } catch (error) {
    return error instanceof ConfigUnavailable;
  }
}

describe("loadConfig", () => {
  test("T-341 the four valid members are accepted", () => {
    expect(parseConfig(VALID, "docchat.example")).toEqual(VALID);
    expect(
      parseConfig({ ...VALID, apiBaseUrl: "https://api.example:8443" }, "docchat.example")
        .apiBaseUrl,
    ).toBe("https://api.example:8443");
  });

  test("T-341 an extra member or a missing member is rejected", () => {
    expect(rejects({ ...VALID, feature: true })).toBe(true);
    const missing = Object.fromEntries(
      Object.entries(VALID).filter(([name]) => name !== "apiScope"),
    );
    expect(rejects(missing)).toBe(true);
    expect(rejects(null)).toBe(true);
    expect(rejects([VALID])).toBe(true);
  });

  test.each([
    ["an upper-case tenant ID", { tenantId: "ABCDEF11-2222-4333-8444-555555555555" }],
    ["an upper-case client ID", { clientId: VALID.clientId.toUpperCase() }],
    ["a malformed UUID", { clientId: "66666666-7777-4888-9999" }],
    ["a UUID with braces", { tenantId: `{${VALID.tenantId}}` }],
    ["a non-string value", { tenantId: 1 }],
  ])("T-341 %s is rejected", (_, change) => {
    expect(rejects({ ...VALID, ...change })).toBe(true);
  });

  test.each([
    ["an http: origin on a page not served from a loopback host", "http://api.example"],
    ["an origin with a path", "https://api.example/v1"],
    ["an origin with a trailing slash", "https://api.example/"],
    ["an origin with a query", "https://api.example?x=1"],
    ["an origin with a fragment", "https://api.example#x"],
    ["an upper-case host", "https://API.example"],
    ["the default port spelled out", "https://api.example:443"],
    ["a user name", "https://user@api.example"],
    ["another scheme", "ftp://api.example"],
    ["text that isn't a URL", "api.example"],
  ])("T-341 %s is rejected", (_, apiBaseUrl) => {
    expect(rejects({ ...VALID, apiBaseUrl })).toBe(true);
  });

  test("T-341 the loopback origin is accepted only on a page served from that loopback host", () => {
    const local = { ...VALID, apiBaseUrl: "http://localhost:5173" };
    expect(parseConfig(local, "localhost").apiBaseUrl).toBe("http://localhost:5173");
    expect(rejects(local, "docchat.example")).toBe(true);
    expect(rejects(local, "127.0.0.1")).toBe(true);
    const harness = { ...VALID, apiBaseUrl: "http://127.0.0.1:4010" };
    expect(parseConfig(harness, "127.0.0.1").apiBaseUrl).toBe("http://127.0.0.1:4010");
    expect(isAllowedApiOrigin("http://127.0.0.2:4010", "127.0.0.2")).toBe(false);
  });

  test.each([
    ["a scope not ending in /access_as_user", "api://tenant/docchat-api/user_impersonation"],
    ["a scope that isn't an api:// URI", "https://tenant/docchat-api/access_as_user"],
    ["a scope with nothing before the suffix", "api:///access_as_user"],
    ["a scope longer than 255 characters", `api://${"a".repeat(240)}/access_as_user`],
    ["a scope with white space", "api://tenant/docchat api/access_as_user"],
  ])("T-341 %s is rejected", (_, apiScope) => {
    expect(rejects({ ...VALID, apiScope })).toBe(true);
  });

  test("T-341 a valid config.json loads", async () => {
    server.use(http.get("*/config.json", () => HttpResponse.json(VALID)));
    await expect(loadConfig("docchat.example")).resolves.toEqual(VALID);
  });

  test.each([
    ["a network error", () => HttpResponse.error()],
    ["a status other than 200", () => HttpResponse.json(VALID, { status: 404 })],
    [
      "invalid JSON",
      () => new HttpResponse("{", { headers: { "Content-Type": "application/json" } }),
    ],
    ["a rule failure", () => HttpResponse.json({ ...VALID, extra: 1 })],
  ])("T-341 %s is config_unavailable", async (_, answer) => {
    server.use(http.get("*/config.json", answer));
    await expect(loadConfig("docchat.example")).rejects.toBeInstanceOf(ConfigUnavailable);
  });

  test("the configuration is read from context inside the app only", () => {
    const wrapper = ({ children }: { children: ReactNode }) => (
      <ConfigContext.Provider value={TEST_CONFIG}>{children}</ConfigContext.Provider>
    );
    expect(renderHook(() => useConfig(), { wrapper }).result.current).toBe(TEST_CONFIG);
    expect(() => renderHook(() => useConfig())).toThrow("useConfig");
  });
});
