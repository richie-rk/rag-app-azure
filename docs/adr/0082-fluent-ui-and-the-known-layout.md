# ADR 0082: Fluent UI and the known layout

- **Status:** Accepted
- **Date:** 2026-10-21
- **Decided by:** Product owner and architect
- **Specified in:** 10-web-client.md, 01-repository.md, 83-infrastructure.md, 84-testing.md
- **Related ADRs:** 0002, 0004, 0011, 0013, 0083, 0084, 0086, 0088, 0089, 0090, 0092

## Context

- The product owner chose Fluent UI for the web client, keeping the screen layout and flows its users know (where each control sits and the order of steps), hosted on Azure Static Web Apps. The client is a React and TypeScript single-page app built with Vite, with Fluent UI React v9 as its only component library, served as static files and calling only the API and Entra ID. Every word of copy is written for this product. The screens are sign-in and access states, Chat, Memory, Projects, Indexes, Documents and Ingestion runs.
- One component library and no run-time assets from other origins keep the bundle, the styling and the Content Security Policy small enough to describe exactly.
- Vendor facts (checked 2026-10-14): `@fluentui/react-components` 9.74.9 is current and peers React below 20; `@fluentui/react-icons` 2.0.344; `@azure/msal-react` 5.7.2 peers React ^19.2.1, so React 19.3.0 is supported; Fluent v9 inserts runtime `<style>` elements through Griffel, and its Vite plugin doesn't remove them (ADR 0083); Fluent components aim at WCAG and WAI-ARIA but don't make a page accessible on their own (ADR 0090); Static Web Apps offers routes, the navigation fallback and global headers on every plan, Free has no SLA and Standard has one, and the configuration file is limited to 20 KB.

## Decision

1. **Libraries, pinned exactly** (no ranges; `.npmrc` sets `save-exact=true` and `engine-strict=true`; installs only with `npm ci` from the committed `package-lock.json`): `react` and `react-dom` 19.3.0; `@fluentui/react-components` 9.74.9; `@fluentui/react-icons` 2.0.344; `@azure/msal-browser` 5.25.1; `@azure/msal-react` 5.7.2; `react-router` 8.4.0; `@tanstack/react-query` 5.104.2; `unified` 11.0.5, `remark-parse` 11.0.0, `remark-gfm` 4.0.1, `remark-rehype` 11.1.2, `rehype-stringify` 10.0.1, `unist-util-visit` 5.1.0; `dompurify` 3.4.17; `zod` 4.6.7. Toolchain: Node.js 24.21.0 LTS (`engines.node` `>=24.15.0 <25`), TypeScript 6.0.3 in strict mode (typescript-eslint 8.71.2 requires a version below 6.1.0), Vite 8.3.4 with `@vitejs/plugin-react` 6.1.2, Vitest 5.0.4 with jsdom 30.1.2 and Testing Library, ESLint 10.12.0 flat configuration, Prettier 3.9.10, `@hey-api/openapi-ts` 0.99.0.
2. **Fluent UI v9 is the only UI system.** Every component comes from `@fluentui/react-components` and every icon from `@fluentui/react-icons`. Styles are written only with Griffel `makeStyles` and `makeResetStyles` and Fluent design tokens: no CSS files, CSS modules, other component libraries or icon fonts. Themes are `webLightTheme` and `webDarkTheme`, unmodified; the theme's system font stack is used, so no font file ships. The Griffel Vite plugin isn't used.
3. **The layout users know.** The app shell has a 48 px top bar (on the left the navigation toggle and the product mark, a link to `/chat`; on the right the theme toggle and the account menu with the user's name, e-mail and sign-out) and a collapsible navigation drawer (for admins an Administration group with Projects, Indexes and Ingestion runs; then Chat and Memory; then the 6 most recent chats and a new-chat item). The chat screen has a left session panel, with the new-chat button above the session list, and a main panel holding, top to bottom, the project picker, the message list, the follow-up suggestions and the composer; the citation viewer opens at the right-hand end. Breakpoints by viewport width:

   | Width | Navigation drawer | Session panel | Citation viewer |
   |---|---|---|---|
   | 1,280 px and wider | `NavDrawer` inline, open by default | inline, 280 px | `InlineDrawer`, 440 px |
   | 768 to 1,279 px | `NavDrawer` overlay, closed by default | inline, 280 px | `OverlayDrawer`, size `medium` |
   | below 768 px | `NavDrawer` overlay, closed by default | hidden; a chats button left of the project picker opens it as an `OverlayDrawer` at the start | `OverlayDrawer`, size `full` |

   The message column is at most 860 px wide and centred; at 320 px nothing scrolls horizontally except tables inside their own scroll regions. The breakpoints keep the main panel at least 360 px wide.
4. **Screens.** The seven screens become S1 to S11 (the access screens; the app shell; Chat; the citation viewer; the feedback dialog; Memory; Projects; New project and Project; Indexes, New index and Index; Documents; Ingestion runs and Ingestion run), routed as ADR 0089 sets. Every screen has one `h1`, uses Fluent v9 components only, and defines four states: Loading, Empty, Error and Partial.
5. **Copy.** The client is English only (`<html lang="en">`). Every user-visible string lives in `src/copy/en.ts` under the keys of the copy catalogue: plain words in the second person that say what happened and what to do next, with no technical terms ("index" only on admin screens, where it is a product term). Two strings are set by the product: "Searching more broadly for better matches" and "This document is already ingested. Update it?". Counts use `Intl.PluralRules`, dates and times `Intl.DateTimeFormat` in the browser's locale, and file sizes are bytes ÷ 1,048,576 with one decimal and the unit MB.
6. **Hosting.** Azure Static Web Apps, Free in `dev` and Standard in `prod`, serving static files only: no linked backend, no managed functions, no app settings, no Static Web Apps authentication and no role assignments; `stagingEnvironmentPolicy: 'Disabled'`, so no preview site with another configuration exists; `allowConfigFileUpdates: true`; public network access enabled.
7. **Nothing from other origins at run time.** No CDN fonts, stylesheets or scripts; no `data:` URLs (`build.assetsInlineLimit` 0); no telemetry SDK or analytics (ADR 0088). The browser calls only the API and Entra ID.
8. **Supported browsers.** The current and previous major versions of Microsoft Edge, Google Chrome, Mozilla Firefox and Apple Safari on desktop, and the current Safari on iOS and Chrome on Android. A browser without `ReadableStream`, `TextDecoder`, `crypto.subtle`, `crypto.randomUUID`, `BroadcastChannel` or `AbortController` gets the unsupported-browser screen before any network call.
9. **Project layout.** `apps/web` (npm package `@docchat/web`) holds `index.html` (one entry), `public/` (`favicon.svg` and `not-found.html` only), `staticwebapp.config.template.json`, `config.example.json`, `scripts/` (`write-env-config.mjs`, `check-dist.mjs`, `gen-limits.mjs`) and `src/` with `bootstrap/`, `platform/`, `api/`, `auth/`, `shell/`, `chat/`, `memory/`, `admin/`, `a11y/`, `copy/`, `ui/` and `test/`; unit and component tests sit beside the module they test.

## Alternatives considered

- **Another component library, or Fluent v9 mixed with v8 or a CSS framework.** Rejected: the product owner chose Fluent UI, and overlapping libraries duplicate components, styles and focus handling; one design system keeps contrast, focus and high-contrast behaviour consistent.
- **A custom brand theme.** Rejected: the unmodified web themes carry Fluent's tested contrast, which the axe checks confirm in both; a custom palette would need its own contrast proof.
- **The Griffel Vite plugin to extract CSS.** Rejected: Fluent still inserts runtime `<style>` elements with it, so the CSP needs the same allowance and the plugin adds nothing.
- **Web fonts, from a CDN or self-hosted.** Rejected: CDN loads would break `font-src 'self'` and tell a third party who uses the app; self-hosting adds bytes for text the system font stack already renders.
- **A server-rendered framework.** Rejected: the API is the only server, and Static Web Apps serves static files only.
- **A different layout.** Rejected: users know this layout and these flows, and the product owner chose to keep them.

## Consequences

- One dependency set for Dependabot and `npm audit`, no run-time request to a third-party origin, and one styling path that the CSP can describe exactly (ADR 0083).
- Fluent's runtime styling needs the CSP's empty-string hash; a Fluent update that changes how it inserts styles is caught by the browser tests' CSP gate before it merges.
- The Free plan in `dev` has no SLA, which `dev` doesn't need; `prod`'s Standard plan has one.
- Obligations: the axe checks on every screen and state in both themes (ADR 0090); the 320 px and 200% zoom checks; the build-output gate (ADR 0086); weekly Dependabot npm updates, where MSAL, Fluent UI, React Router, DOMPurify, Zod and the `unified` family merge only after the full browser suite passes (86-operations.md).

## References

- `@fluentui/react-components` 9.74.9: https://www.npmjs.com/package/@fluentui/react-components/v/9.74.9 (checked 2026-10-14)
- Fluent UI v9 accessibility overview: https://react.fluentui.dev/?path=/docs/concepts-developer-accessibility-components-overview--docs (checked 2026-10-14)
- `@griffel/vite-plugin` 1.0.1: https://www.npmjs.com/package/@griffel/vite-plugin/v/1.0.1 (checked 2026-10-14)
- `@azure/msal-react` 5.7.2: https://www.npmjs.com/package/@azure/msal-react/v/5.7.2 (checked 2026-10-14)
- Vite build options: https://vite.dev/config/build-options (checked 2026-10-14)
- Static Web Apps configuration: https://learn.microsoft.com/en-us/azure/static-web-apps/configuration (checked 2026-10-14)
- Static Web Apps plans: https://learn.microsoft.com/en-us/azure/static-web-apps/plans (checked 2026-10-14)
- `Microsoft.Web/staticSites` resource: https://learn.microsoft.com/en-us/azure/templates/microsoft.web/staticsites (checked 2026-10-14)
