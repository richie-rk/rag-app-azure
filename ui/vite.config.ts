import { defineConfig, loadEnv, type Plugin } from "vite";
import react from "@vitejs/plugin-react";

// Defense-in-depth against injected markup: no inline/eval script can run and
// plugins cannot be loaded. style-src needs 'unsafe-inline' for Fluent UI's
// runtime-injected styles. frame-ancestors can't be set from a <meta> tag; set
// it (or X-Frame-Options) on the hosting web app.
//
// connect-src is derived from the VITE_*_API_URL values present at build time
// (plus the Microsoft identity/Graph endpoints MSAL talks to), so the deployed
// bundle can only reach its own configured APIs and an injected same-origin
// script cannot exfiltrate to an arbitrary HTTPS host. When no API URLs are
// configured (e.g. a bare local build), it falls back to 'self' https: rather
// than producing a bundle that cannot call its APIs at all.
function buildCsp(env: Record<string, string>): string {
  const apiOrigins = [
    env.VITE_CHAT_API_URL,
    env.VITE_UTILS_API_URL,
    env.VITE_INGESTION_API_URL,
  ]
    .filter(Boolean)
    .map((url) => {
      try {
        return new URL(url).origin;
      } catch {
        return null; // relative URLs ("/api") are same-origin, covered by 'self'
      }
    })
    .filter((origin): origin is string => Boolean(origin));

  const connectSrc = apiOrigins.length
    ? [
        "'self'",
        ...new Set(apiOrigins),
        "https://login.microsoftonline.com",
        "https://graph.microsoft.com",
      ].join(" ")
    : "'self' https:";

  return [
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob:",
    "font-src 'self' data:",
    `connect-src ${connectSrc}`,
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
  ].join("; ");
}

// Build-only: the dev server relies on an inline react-refresh preamble that a
// script-src 'self' policy would block.
function injectCsp(csp: string): Plugin {
  return {
    name: "inject-csp",
    apply: "build",
    transformIndexHtml(html) {
      return {
        html,
        tags: [
          {
            tag: "meta",
            attrs: { "http-equiv": "Content-Security-Policy", content: csp },
            injectTo: "head-prepend",
          },
        ],
      };
    },
  };
}

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, __dirname, "VITE_");
  return {
    plugins: [react(), injectCsp(buildCsp(env))],
    server: {
      port: 5173,
      proxy: {
        "/api": {
          target: "http://localhost:7071",
          changeOrigin: true,
        },
        "/chat": {
          target: "http://localhost:8000",
          changeOrigin: true,
        },
      },
    },
  };
});
