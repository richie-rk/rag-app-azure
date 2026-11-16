# ADR 0083: Static site headers and Content Security Policy

- **Status:** Accepted
- **Date:** 2026-10-21
- **Decided by:** Product owner and architect
- **Specified in:** 10-web-client.md, 82-security.md, 83-infrastructure.md, 84-testing.md
- **Related ADRs:** 0004, 0018, 0082, 0084, 0085, 0086, 0087, 0088, 0092

## Context

- Access and refresh tokens live in the browser's session storage (ADR 0004), so script injection is the threat that matters most; the Content Security Policy is one of the defences, with sanitised rendering (ADR 0084) and validated streams (ADR 0085).
- The product's policy: `default-src 'none'`; scripts and fonts only from the site; images from the site, `data:` and `blob:`; fetches only to the site, the API origin and `https://login.microsoftonline.com`; no plugins; `base-uri 'self'`; no form submissions; no framing of the app; plus `Strict-Transport-Security`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: strict-origin-when-cross-origin` and a restrictive `Permissions-Policy`, a navigation fallback to the app shell, and Static Web Apps' built-in sign-in routes blocked.
- Sign-in uses full-page redirects only (ADR 0018). Silent renewal in a hidden iframe would need `frame-src 'self'` and a redirect page that may be framed, because Entra ID redirects the iframe to the app's own redirect URI; the product owner chose no hidden-iframe renewal, so frames are needed only for citation PDFs shown from `blob:` URLs. Voice input is out of scope, so the microphone is denied.
- Vendor facts (checked 2026-10-14): Static Web Apps `globalHeaders` are static strings with no templating or nonce generation, a route header with the same name overrides a global one, route rules don't apply to responses that trigger the navigation fallback, and the configuration file is limited to 20 KB; a CSP nonce must be fresh for every response, which static headers can't do; `'self'` never matches `blob:` URLs, and a document loaded from a `blob:` URL inherits its creator's policy; `frame-ancestors` doesn't fall back to `default-src` and is ignored in a `<meta>` policy. Measured in Edge 154 and Chrome 155: Fluent v9 creates empty `<style>` elements and fills them through the CSSOM, so `style-src 'self'` alone leaves the app unstyled with 12 violations, while the SHA-256 of the empty string gives full theming with zero violations; Zod probes `new Function` unless set to jitless; a Vite production build emits only external module scripts; a PDF blob in an unsandboxed iframe renders in the built-in viewer under this policy with zero violations.

## Decision

1. **One template.** `apps/web/staticwebapp.config.template.json` is the only source of the site's rules and headers; the deploy-time generator (ADR 0086) writes it per environment, replacing the single placeholder `__API_ORIGIN__` with `new URL(API_BASE_URL).origin` and failing above 20,480 bytes. `trailingSlash` is left unset, so `/not-found.html` is served at its own path. Exactly:

   ```json
   {
     "routes": [
       { "route": "/.auth/login/aad", "statusCode": 404 },
       { "route": "/.auth/login/github", "statusCode": 404 },
       { "route": "/.auth/*", "statusCode": 404 },
       { "route": "/assets/*", "headers": { "Cache-Control": "public, max-age=31536000, immutable" } }
     ],
     "navigationFallback": {
       "rewrite": "/index.html",
       "exclude": [
         "/assets/*",
         "/.auth/*",
         "/config.json",
         "/favicon.svg",
         "/not-found.html",
         "/*.{js,mjs,css,map,json,txt,xml,ico,png,jpg,svg,webmanifest}"
       ]
     },
     "responseOverrides": {
       "404": { "rewrite": "/not-found.html", "statusCode": 404 }
     },
     "globalHeaders": {
       "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self' 'sha256-47DEQpj8HBSa+/TImW+5JCeuQeRkm5NMpJWZG3hSuFU='; img-src 'self' data: blob:; font-src 'self'; connect-src 'self' __API_ORIGIN__ https://login.microsoftonline.com; frame-src blob:; object-src 'none'; base-uri 'self'; form-action 'none'; frame-ancestors 'none'",
       "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
       "X-Content-Type-Options": "nosniff",
       "Referrer-Policy": "strict-origin-when-cross-origin",
       "Permissions-Policy": "accelerometer=(), autoplay=(), camera=(), clipboard-read=(), clipboard-write=(self), display-capture=(), encrypted-media=(), fullscreen=(), geolocation=(), gyroscope=(), hid=(), idle-detection=(), magnetometer=(), microphone=(), midi=(), payment=(), picture-in-picture=(), publickey-credentials-get=(), screen-wake-lock=(), serial=(), usb=(), web-share=(), xr-spatial-tracking=()",
       "Cross-Origin-Opener-Policy": "same-origin",
       "Cross-Origin-Resource-Policy": "same-origin",
       "X-Robots-Tag": "noindex, nofollow",
       "Cache-Control": "no-store"
     },
     "mimeTypes": { ".json": "application/json" }
   }
   ```

2. **The CSP's two choices.** `frame-src blob:` only, because sign-in never uses a hidden iframe; `style-src 'self'` plus the empty-string hash `'sha256-47DEQpj8HBSa+/TImW+5JCeuQeRkm5NMpJWZG3hSuFU='`, which admits exactly the empty `<style>` elements Fluent fills through the CSSOM. No `'unsafe-inline'`, `'unsafe-eval'`, `'wasm-unsafe-eval'`, `report-uri` or `report-to`; Trusted Types aren't enforced.
3. **Permissions-Policy.** Every listed feature is denied to the page and all frames except `clipboard-write=(self)`, which the copy-answer action needs. Only features current Chromium recognises are listed, so the header logs nothing (`browsing-topics` and `interest-cohort` are left out); `microphone=()` because voice input is out of scope.
4. **Other headers.** HSTS `max-age=31536000; includeSubDomains` without `preload`, because the default host belongs to Microsoft's domain; `nosniff`; `Referrer-Policy: strict-origin-when-cross-origin`; `Cross-Origin-Opener-Policy: same-origin`, safe because sign-in uses full-page redirects only; `Cross-Origin-Resource-Policy: same-origin`; `X-Robots-Tag: noindex, nofollow`; `Cache-Control: no-store` on every response except the content-hashed `/assets/*` files, so a deploy takes effect on the next load and the redirect URI's page (`/`) is never served from a cache.
5. **Blocked routes and 404s.** `/.auth/login/aad`, `/.auth/login/github` and everything else under `/.auth/` answer `404` with `not-found.html`; the app uses none of Static Web Apps' built-in authentication. Every navigation to a path that isn't a file gets `index.html` with `200`; a missing file under an excluded pattern gets `404` with `not-found.html`, so a script missing after a deploy fails cleanly instead of returning HTML. `public/not-found.html` is static HTML with no script or style.
6. **What keeps the policy sufficient.** Vite `build.assetsInlineLimit` 0 (no `data:` fonts or images) and `build.sourcemap` false; `z.config({ jitless: true })` is the first import of `src/main.tsx` and `z.compile()` is banned; `scripts/check-dist.mjs` fails the build on any HTML file with an inline `<script>` without `src`, a `<style>` element or a `style=` attribute; MSAL runs with `allowRedirectInIframe: false` and never calls a popup, `ssoSilent` or a hidden-iframe flow, so no redirect-bridge page exists (ADR 0018); the redirect URI `/` is `index.html`, a real file served with `no-store`.
7. **Tests.** Every browser test, run against the production build on the Static Web Apps CLI 2.0.10 emulator with stubs (ADR 0092), fails on any `securitypolicyviolation` event, any console error and any Permissions-Policy warning; a browser test checks that every response carries exactly these headers with the configured API origin; after every web deploy to `dev` and `prod`, the deployed smoke tests check every header against the template with that environment's API origin, that `/.auth/login/aad`, `/.auth/login/github` and `/.auth/me` answer `404` with no `Location` and no `Set-Cookie`, that `/assets/missing.js` answers `404`, and that before the sign-in navigation there is no CSP violation and no request to any other origin; a unit test parses Zod schemas under the CSP with no `script-src` violation.

## Alternatives considered

- **`'unsafe-inline'` for styles.** Rejected: it would admit any injected `<style>` element or `style=` attribute, while the hash admits only empty elements.
- **Nonces.** Rejected: static headers can't carry a fresh nonce per response.
- **Allowing hidden-iframe renewal (`frame-src 'self' blob: https://login.microsoftonline.com` and a redirect-bridge page with `frame-ancestors 'self'` and no COOP).** Rejected by the product owner: single-page-app refresh tokens last 24 hours and a top-level visit to Entra ID is required after that anyway, iframe renewal fails where third-party cookies are blocked, and the exceptions would weaken framing protection.
- **A `<meta>` policy in `index.html`.** Rejected: `frame-ancestors` is ignored there, and headers also cover `config.json` and `not-found.html`.
- **Violation reports to an endpoint.** Rejected: they would be client telemetry, which ADR 0088 excludes; the browser tests catch violations instead.
- **Trusted Types.** Rejected: every library would need typed sinks, which Fluent and MSAL don't guarantee; one insertion point and lint rules confine HTML sinks instead (ADR 0084).
- **HSTS `preload`.** Rejected: the default host is under a domain the app doesn't control.

## Consequences

- An injected script, style, frame, form target, object or remote image is refused by the browser, and `connect-src` keeps every fetch on the site, the API and Entra ID, so injected text can't send data to another origin.
- A new origin (an API custom domain, another identity endpoint) needs a template change and a web redeploy; changing the API's origin means deploying the API on the new origin first, then redeploying the web client so `config.json` and the CSP change together, then running the smoke tests and retiring the old origin.
- `img-src` keeps `data:` and `blob:`; answers never render images anyway (ADR 0084).
- Risks: a library update that inserts non-empty inline styles or evaluates code fails the browser suite before it merges; a Chromium release that stops recognising a listed Permissions-Policy feature is handled by removing that feature from the template in the next release (86-operations.md).
- Obligations: the headers and the policy as testable statements in 82-security.md; the deployed smoke tests in `dev` and `prod` after every web deploy.

## References

- Content Security Policy Level 3: https://www.w3.org/TR/CSP3/ (checked 2026-10-14)
- `style-src`: https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Content-Security-Policy/style-src (checked 2026-10-14)
- Static Web Apps configuration: https://learn.microsoft.com/en-us/azure/static-web-apps/configuration (checked 2026-10-14)
- Static Web Apps authentication and authorization (blocking the built-in providers): https://learn.microsoft.com/en-us/azure/static-web-apps/authentication-authorization (checked 2026-10-14)
- MSAL.js hidden iframes and the redirect URI: https://learn.microsoft.com/en-us/entra/identity-platform/msal-js-avoid-page-reloads (checked 2026-10-14)
- Third-party cookie blocking and single-page apps: https://learn.microsoft.com/en-us/entra/identity-platform/reference-third-party-cookies-spas (checked 2026-10-14)
- `@griffel/vite-plugin` 1.0.1: https://www.npmjs.com/package/@griffel/vite-plugin/v/1.0.1 (checked 2026-10-14)
- Zod jitless mode: https://zod.dev/compile (checked 2026-10-14)
- Vite features (production script output): https://vite.dev/guide/features (checked 2026-10-14)
