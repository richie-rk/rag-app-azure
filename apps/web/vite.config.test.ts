// @vitest-environment node
import { mkdtempSync, writeFileSync } from "node:fs";
import type { IncomingMessage, ServerResponse } from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { build, type Plugin, type Rolldown, type UserConfig, type ViteDevServer } from "vite";
import { describe, expect, test, vi } from "vitest";

async function loadConfig(): Promise<UserConfig> {
  vi.resetModules();
  return (await import("./vite.config")).default;
}

const DEV_TARGET = "https://app-docchat-api-dev-x1y2z3.example.test";

describe("vite.config.ts", () => {
  test("T-344 proxies /v1 and /healthz to 127.0.0.1:8000 by default", async () => {
    vi.stubEnv("DOCCHAT_DEV_API_TARGET", undefined);
    const config = await loadConfig();
    expect(config.server?.proxy).toEqual({
      "/v1": "http://127.0.0.1:8000",
      "/healthz": "http://127.0.0.1:8000",
    });
    expect(config.server?.port).toBe(5173);
    expect(config.server?.strictPort).toBe(true);
  });

  test("T-344 proxies to DOCCHAT_DEV_API_TARGET when it is set", async () => {
    vi.stubEnv("DOCCHAT_DEV_API_TARGET", DEV_TARGET);
    const config = await loadConfig();
    expect(config.server?.proxy).toEqual({ "/v1": DEV_TARGET, "/healthz": DEV_TARGET });
  });

  test("T-344 the production build contains neither value", async () => {
    vi.stubEnv("DOCCHAT_DEV_API_TARGET", DEV_TARGET);
    const output = (await build({
      configFile: join(import.meta.dirname, "vite.config.ts"),
      root: import.meta.dirname,
      logLevel: "silent",
      build: { write: false },
    })) as Rolldown.RolldownOutput | Rolldown.RolldownOutput[];
    const files = (Array.isArray(output) ? output : [output]).flatMap((each) => each.output);
    expect(files.length).toBeGreaterThan(1);
    for (const file of files) {
      const text = file.type === "chunk" ? file.code : String(file.source);
      expect(text, file.fileName).not.toContain(DEV_TARGET);
      expect(text, file.fileName).not.toContain("127.0.0.1:8000");
      expect(text, file.fileName).not.toContain("DOCCHAT_DEV_API_TARGET");
      expect(file.fileName.endsWith(".map"), file.fileName).toBe(false);
    }
  }, 120_000);

  test("the build settings are section 1.5's", async () => {
    const config = await loadConfig();
    expect(config.build).toEqual({
      outDir: "dist",
      emptyOutDir: true,
      target: "es2023",
      sourcemap: false,
      assetsInlineLimit: 0,
      modulePreload: { polyfill: false },
      cssCodeSplit: true,
    });
    expect(config.test).toMatchObject({
      environment: "jsdom",
      setupFiles: ["src/test/setup.ts"],
      restoreMocks: true,
      unstubGlobals: true,
      coverage: {
        provider: "v8",
        include: ["src/**"],
        thresholds: { lines: 85, statements: 85, functions: 85, branches: 80 },
      },
    });
  });
});

describe("the serve-only config.json plugin", () => {
  function middlewareOf(root: string) {
    const config = { plugins: [] as Plugin[] };
    let handler:
      ((req: IncomingMessage, res: ServerResponse, next: () => void) => void) | undefined;
    const server = {
      config: { root },
      middlewares: {
        use: (fn: typeof handler) => {
          handler = fn;
        },
      },
    } as unknown as ViteDevServer;
    return { server, config, handler: () => handler };
  }

  function respond() {
    const headers: Record<string, string> = {};
    let resolveEnd: (body: unknown) => void = () => undefined;
    const ended = new Promise((resolve) => {
      resolveEnd = resolve;
    });
    const response = {
      statusCode: 0,
      setHeader: (name: string, value: string) => {
        headers[name] = value;
      },
      end: (body?: unknown) => {
        resolveEnd(body);
      },
    };
    return { response, headers, ended };
  }

  test("answers GET /config.json from config.local.json in development only", async () => {
    const { localConfigPlugin } = await import("./vite.config");
    const plugin = localConfigPlugin();
    expect(plugin.apply).toBe("serve");
    const root = mkdtempSync(join(tmpdir(), "docchat-vite-"));
    writeFileSync(join(root, "config.local.json"), '{"tenantId":"x"}');
    const { server, handler } = middlewareOf(root);
    (plugin.configureServer as (server: ViteDevServer) => void)(server);

    const ok = respond();
    handler()?.(
      { method: "GET", url: "/config.json?t=1" } as IncomingMessage,
      ok.response as unknown as ServerResponse,
      () => undefined,
    );
    expect(String(await ok.ended)).toBe('{"tenantId":"x"}');
    expect(ok.response.statusCode).toBe(200);
    expect(ok.headers).toEqual({ "Content-Type": "application/json", "Cache-Control": "no-store" });

    const next = vi.fn();
    handler()?.(
      { method: "GET", url: "/other" } as IncomingMessage,
      respond().response as unknown as ServerResponse,
      next,
    );
    handler()?.(
      { method: "POST", url: "/config.json" } as IncomingMessage,
      respond().response as unknown as ServerResponse,
      next,
    );
    handler()?.(
      { method: "GET" } as IncomingMessage,
      respond().response as unknown as ServerResponse,
      next,
    );
    expect(next).toHaveBeenCalledTimes(3);
  });

  test("a missing config.local.json answers 404, which shows the Configuration error screen", async () => {
    const { localConfigPlugin } = await import("./vite.config");
    const plugin = localConfigPlugin();
    const { server, handler } = middlewareOf(mkdtempSync(join(tmpdir(), "docchat-vite-")));
    (plugin.configureServer as (server: ViteDevServer) => void)(server);
    const missing = respond();
    handler()?.(
      { method: "GET", url: "/config.json" } as IncomingMessage,
      missing.response as unknown as ServerResponse,
      () => undefined,
    );
    await missing.ended;
    expect(missing.response.statusCode).toBe(404);
  });
});
