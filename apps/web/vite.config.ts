/// <reference types="vitest/config" />
import { readFile } from "node:fs/promises";
import { join } from "node:path";

import react from "@vitejs/plugin-react";
import { defineConfig, type Plugin } from "vite";

// Read here only, never by the bundle: the development server proxies the API through the page's
// own origin, so no environment needs a localhost CORS rule. 127.0.0.1 rather than localhost,
// because localhost can resolve to ::1, where an API listening on IPv4 alone doesn't answer.
const target = process.env["DOCCHAT_DEV_API_TARGET"] ?? "http://127.0.0.1:8000";

/** Answers `GET /config.json` from `config.local.json` in development only, so the build output
 * never contains a configuration file. A missing file answers 404, which the client shows as the
 * Configuration error screen. */
export function localConfigPlugin(): Plugin {
  return {
    name: "docchat-local-config",
    apply: "serve",
    configureServer(server) {
      server.middlewares.use((request, response, next) => {
        const path = (request.url ?? "").split("?")[0];
        if (request.method !== "GET" || path !== "/config.json") {
          next();
          return;
        }
        readFile(join(server.config.root, "config.local.json"))
          .then((body) => {
            response.statusCode = 200;
            response.setHeader("Content-Type", "application/json");
            response.setHeader("Cache-Control", "no-store");
            response.end(body);
          })
          .catch(() => {
            response.statusCode = 404;
            response.end();
          });
      });
    },
  };
}

export default defineConfig({
  plugins: [react(), localConfigPlugin()],
  build: {
    outDir: "dist",
    emptyOutDir: true,
    target: "es2023",
    sourcemap: false,
    // No data: URL is ever emitted, because font-src 'self' allows none.
    assetsInlineLimit: 0,
    // No inline polyfill script, so script-src 'self' suffices.
    modulePreload: { polyfill: false },
    cssCodeSplit: true,
  },
  server: {
    port: 5173,
    strictPort: true,
    proxy: { "/v1": target, "/healthz": target },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["src/test/setup.ts"],
    restoreMocks: true,
    unstubGlobals: true,
    unstubEnvs: true,
    // Fluent component tests run slowly while every file runs in parallel; 5 s is too tight.
    testTimeout: 15_000,
    coverage: {
      provider: "v8",
      // The summary goes to the console; the working files stay out of the repository's tree,
      // whose .gitignore doesn't list a coverage directory.
      reporter: ["text"],
      reportsDirectory: "node_modules/.vitest/coverage",
      include: ["src/**"],
      exclude: [
        "src/api/generated/**",
        "src/main.tsx",
        "src/test/**",
        "src/**/*.test.{ts,tsx}",
        "src/**/*.d.ts",
      ],
      thresholds: {
        lines: 85,
        statements: 85,
        functions: 85,
        branches: 80,
        "src/chat/stream/**": { branches: 95 },
        "src/chat/render/**": { branches: 95 },
        "src/api/http.ts": { branches: 95 },
        "src/platform/traceparent.ts": { branches: 95 },
        "src/bootstrap/**": { branches: 95 },
      },
    },
  },
});
