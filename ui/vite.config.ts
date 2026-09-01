import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";

// Defense-in-depth against injected markup: no inline/eval script can run and
// plugins cannot be loaded. connect-src allows any https origin because the
// chat/utils/ingestion API hosts are env-configured at deploy time, but plain
// http (and ws) exfiltration is still blocked. style-src needs 'unsafe-inline'
// for Fluent UI's runtime-injected styles. frame-ancestors can't be set from a
// <meta> tag; set it (or X-Frame-Options) on the hosting web app.
const CSP = [
  "default-src 'self'",
  "script-src 'self'",
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data: blob:",
  "font-src 'self' data:",
  "connect-src 'self' https:",
  "object-src 'none'",
  "base-uri 'self'",
  "form-action 'self'",
].join("; ");

// Build-only: the dev server relies on an inline react-refresh preamble that a
// script-src 'self' policy would block.
function injectCsp(): Plugin {
  return {
    name: "inject-csp",
    apply: "build",
    transformIndexHtml(html) {
      return {
        html,
        tags: [
          {
            tag: "meta",
            attrs: { "http-equiv": "Content-Security-Policy", content: CSP },
            injectTo: "head-prepend",
          },
        ],
      };
    },
  };
}

export default defineConfig({
  plugins: [react(), injectCsp()],
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
});
