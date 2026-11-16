# ADR 0018: Redirect-only browser sign-in without hidden iframes

- **Status:** Accepted
- **Date:** 2026-10-18
- **Decided by:** Product owner and architect
- **Specified in:** 10-web-client.md, 82-security.md, 84-testing.md, 86-operations.md
- **Related ADRs:** 0004, 0016, 0017, 0083, 0084, 0085, 0086, 0087, 0091, 0092

## Context

DocChat's web client signs users in with MSAL for React, using the authorization code flow with PKCE. The token cache lives in session storage, never in local storage, so tokens don't outlive the tab or spread across tabs. The client requests only the API scope and sends the access token as a bearer header on every API call; it holds no other token and no Azure credential. ADR 0004 records the cost of keeping tokens in the browser: script injection could read them, so the defences of ADRs 0083 to 0085 are critical.

The client needs a clear state for each sign-in outcome (signed out, not assigned to the app, no projects), never an endless spinner or an exception, and a sign-out that clears MSAL's cache, ends the Entra ID session (a shared device must not sign the next person in silently) and returns to the signed-out state.

Silent renewal in a hidden iframe can't work for this site. The site's CSP allows frames only from `blob:` (the citation viewer) and every page sends `frame-ancestors 'none'`, so the iframe that Entra ID redirects back to the app's redirect URI can't render; CSP is checked on every redirect. Hidden-iframe renewal also fails wherever third-party cookies are blocked, and MSAL v5 then stalls for its 10-second `iframeBridgeTimeout` and fails with `timed_out` instead of an interaction-required error. The product owner chose renewal by refresh token, then a top-level redirect, with no hidden iframe at all.

Vendor facts (Microsoft Learn, npm and the MSAL documentation, confirmed 2026-10-14):

- `@azure/msal-browser` 5.25.1 and `@azure/msal-react` 5.7.2 are current (v5 line). `cacheLocation: 'sessionStorage'` lasts until the tab closes and isn't shared across tabs; MSAL calls browser storage secure "as long as your application does not have cross-site scripting".
- `acquireTokenSilent` tries a cached access token, then the refresh token at the token endpoint over `fetch`, then a hidden iframe once the refresh token has expired. `CacheLookupPolicy.AccessTokenAndRefreshToken` never uses the iframe. Refresh tokens issued to `spa` redirect URIs expire 24 hours after sign-in, and renewal doesn't extend that (`AADSTS700084`); after it an SPA must get a new authorization code through a top-level visit to the sign-in page.
- MSAL v5's redirect bridge is needed only for popup and hidden-iframe flows; redirect flows work without it. Apps that use redirect APIs must call `handleRedirectPromise()`; `createStandardPublicClientApplication` creates an initialised instance.
- `logoutRedirect()` clears the user's cache entries, navigates to the Entra ID sign-out endpoint (adding `logout_hint` itself) and returns to `postLogoutRedirectUri`. MSAL's metadata, discovery and token calls need `connect-src https://login.microsoftonline.com`; `/authorize` and `/logout` are top-level navigations.

## Decision

1. **Library and instance.** `@azure/msal-browser` 5.25.1 and `@azure/msal-react` 5.7.2, pinned exactly. One instance per page load, created with `createStandardPublicClientApplication` from exactly this configuration:

   ```ts
   {
     auth: {
       clientId: config.clientId,
       authority: `https://login.microsoftonline.com/${config.tenantId}`,
       redirectUri: `${window.location.origin}/`,
       postLogoutRedirectUri: `${window.location.origin}/`,
     },
     cache: { cacheLocation: 'sessionStorage' },
     system: {
       allowRedirectInIframe: false,
       loggerOptions: { logLevel: LogLevel.Error, piiLoggingEnabled: false, loggerCallback: devLog },
     },
   }
   ```

   `config` is the deploy-time `config.json` (ADR 0086). The authority is tenant-specific, never `common` or `organizations`. `devLog` writes to the console only when `import.meta.env.DEV` is true and does nothing in production builds. The redirect URI follows the page's own origin, so the default host and every registered custom domain work without a rebuild.
2. **Protocol and scopes.** MSAL runs the authorization code flow with PKCE (S256). Every interactive and silent request names exactly one scope, `config.apiScope`; MSAL adds `openid`, `profile` and `offline_access`. The access token goes only into the `Authorization: Bearer` header of API calls; it is never logged, stored by the app or put in a URL, and the client never decodes token claims: role and projects come from `getCurrentUser` and `listMyProjects` (ADR 0091).
3. **Redirect flows only.** No popup API, `ssoSilent` or hidden-iframe flow is ever called, so no redirect-bridge page exists. The CSP keeps `frame-src blob:` only and `frame-ancestors 'none'`, `connect-src` names `https://login.microsoftonline.com`, and the site sends `Cross-Origin-Opener-Policy: same-origin` (ADR 0083). The redirect URI is the real file `/` (`index.html`), served with `Cache-Control: no-store`.
4. **Startup.** A browser without `ReadableStream`, `TextDecoder`, `crypto.subtle`, `crypto.randomUUID`, `BroadcastChannel` or `AbortController` shows the Unsupported browser state before any network call. Otherwise: load `config.json`; create the instance; await `handleRedirectPromise({ navigateToLoginRequestUrl: false })` before the router renders. The active account is the redirect result's account, else the first cached account whose `tenantId` equals `config.tenantId`; accounts of other tenants are ignored. With no account: when the session-storage key `docchat.signedOut` is absent, call `loginRedirect({ scopes: [config.apiScope], state })` at once; when it is `1`, show the Signed out state, whose Sign in action removes the key and calls `loginRedirect`.
5. **Return path.** Every interactive request carries `state` = the in-app path to return to (`pathname` plus `search`). After a redirect result the app replaces the location with `state` only when it starts with exactly one `/`, contains no `\`, is at most 512 characters and resolves to the app's own origin; otherwise with `/chat`.
6. **Getting tokens.** Before every API call: `acquireTokenSilent({ scopes: [config.apiScope], account, cacheLookupPolicy: CacheLookupPolicy.AccessTokenAndRefreshToken })`, which returns the cached access token or redeems the refresh token over `fetch` and never uses an iframe. MSAL renews an access token 300 seconds before it expires (its default offset), and concurrent callers share one pending promise.
7. **The `401` retry.** When the API answers `401` (problem code `invalid_token`, or a `401` without a problem body, which comes from platform authentication), the client repeats that request once after `acquireTokenSilent({ scopes: [config.apiScope], account, cacheLookupPolicy: CacheLookupPolicy.RefreshToken })`, which skips the cached access token and also never uses an iframe. A second `401`, or an `InteractionRequiredAuthError` from either call (`AADSTS700084` included), means the session has expired.
8. **Session expiry.** Before the shell has rendered, the client calls `acquireTokenRedirect({ scopes: [config.apiScope], account, state })` at once, because nothing is on screen to lose. After it has rendered, the operation that needed the token shows its own error and the Session expired bar appears above the main content; its Sign in again action calls `acquireTokenRedirect` with `state` = the current path. The client never redirects on its own once the shell is on screen, so the user can copy unsent text first; until then every new API call fails at once with the same state, and streams already running continue. One guard shared by the token module makes concurrent expiries share one redirect. Any other MSAL error is shown as a retryable error with MSAL's correlation ID and never starts a redirect.
9. **Sign-out.** Sign out in the account menu asks for confirmation when a stream or upload is active, then: (1) sets session-storage `docchat.signedOut` to `1`; (2) posts `{ "type": "signed-out" }` on the `BroadcastChannel` named `docchat-auth`; (3) aborts active streams and uploads and calls `queryClient.clear()`; (4) calls `logoutRedirect({ account, postLogoutRedirectUri: '<origin>/' })`, which clears MSAL's cache for the account, ends the Entra ID session and returns to `/`, where the Signed out state shows. Every other tab that receives the message aborts its streams and uploads, calls `queryClient.clear()` and MSAL's `clearCache()`, sets its own flag and shows Signed out, because session storage is per tab.
10. **Sign-in outcomes.** `handleRedirectPromise` errors and API answers map to states (ADR 0091 renders them):

    | Signal | State |
    |---|---|
    | MSAL `AuthError` whose `errorNo` is `50105` or whose message contains `AADSTS50105`, or an API `403` with code `role_not_assigned` | Not assigned, naming the account; Try again calls `loginRedirect` with `prompt: 'select_account'` |
    | MSAL error containing `AADSTS65001` or `AADSTS90094` | Consent missing (the tenant-wide consent of ADR 0016 hasn't been granted) |
    | MSAL `user_cancelled`, or `access_denied` without `AADSTS50105` | Signed out |
    | `listMyProjects` returns no items on its first page | No projects (contact an admin) |
    | A `403` without a problem body (platform authentication) | HTTP failure with the request's trace ID, never Not assigned |
    | Any other MSAL error | Sign-in error with MSAL's correlation ID |
11. **Verification.** Browser tests in `tests/e2e/specs/web/identity.spec.ts` run the production build, MSAL and the CSP unchanged against the stub identity provider and stub API (ADR 0092), and every case fails on any CSP violation: (a) after sign-in every `localStorage` key is one of the two preference keys (`docchat.theme`, `docchat.nav.open`) and no `localStorage` value holds a token the stub issued; a reload keeps the user signed in with no `/authorize` request, while clearing `sessionStorage` alone and reloading goes straight to `/authorize`; (b) Sign out sets the flag, navigates the top-level page to the stub's `/oauth2/v2.0/logout` with `post_logout_redirect_uri` = `<origin>/`, and afterwards no `sessionStorage` value holds a token, Signed out shows and no `/authorize` follows; (c) `AADSTS50105` and `403 role_not_assigned` each show Not assigned with no further API call, a bare `403` shows the account-load failure with its trace ID and Try again, and an empty first page shows No projects; (d) with the stub's `/token` answering without `refresh_token`, the next call's bare `401` leads through the one `RefreshToken` retry to `InteractionRequiredAuthError`, Session expired shows, and Sign in again navigates the top-level page to `/authorize` without `prompt=none`; throughout, `page.frames()` holds only the main frame and a `MutationObserver` installed with `addInitScript` records no `iframe` element. A manual release check with a real account against the real Entra ID confirms that, after Sign out, the next sign-in asks for an account instead of signing in silently.

## Alternatives considered

- **Hidden-iframe silent renewal.** It needs `frame-src 'self' blob: https://login.microsoftonline.com`, a redirect-bridge page served as a real file with its own `frame-ancestors 'self'` and no COOP, and handling of MSAL v5's 10-second `timed_out`; it still fails wherever third-party cookies are blocked, and it weakens the site-wide framing and opener protections.
- **Popup flows.** They need a bridge page without COOP, which conflicts with the site's `Cross-Origin-Opener-Policy: same-origin`, and they fail under popup blockers.
- **Local storage for the cache.** Tokens would outlive the browser session and be shared by every tab, widening what one script injection or one shared device exposes.
- **Memory storage for the cache.** Every page reload would lose the session and cost a sign-in redirect; session storage with the defences of ADRs 0083 to 0085 is the trade-off ADR 0004 accepts.
- **`forceRefresh: true` for the `401` retry.** Once the refresh token has expired MSAL falls back to the hidden iframe, which the CSP blocks; `CacheLookupPolicy.RefreshToken` never uses one.
- **Redirecting automatically whenever the session expires.** It would discard unsent text; the Session expired bar waits for the user except at startup, when nothing is on screen.
- **A server-side session cookie with tokens held by a backend.** It needs the web client and the API on one origin, which ADR 0004 rejects for this design, and the browser holding only the API token is the sign-in model this product uses.

## Consequences

- The CSP has no identity-provider frame source and no bridge page exists; sign-in behaves the same in every browser, including those that block third-party cookies, and one sign-in costs one page load.
- A tab left open for more than 24 hours needs one top-level visit to Entra ID when its refresh token expires; with a live Entra ID session the visit returns without a prompt. Each new tab starts with its own sign-in redirect, because session storage isn't shared across tabs.
- Signing out ends the browser's Entra ID session, so the next sign-in in that browser asks for an account, which a shared device needs.
- Platform authentication's bare `401` is handled by the same single retry as the app's `invalid_token`, and its bare `403` is never mistaken for a missing role (ADR 0017).
- Obligations: the browser tests of item 11; the stub identity provider's per-case option to answer `/token` without `refresh_token`; the manual check in the release sign-in procedure (86-operations.md); exact MSAL pins, so an upgrade must pass the no-iframe test before it ships.
- Risk: script injection could read tokens from session storage while the tab is open; ADR 0004 accepts it and ADRs 0083 to 0085 are the defences.

## References

- MSAL Browser and React packages: https://www.npmjs.com/package/@azure/msal-browser, https://www.npmjs.com/package/@azure/msal-react/v/5.7.2 (checked 2026-10-14)
- MSAL Browser caching, token lifetimes, redirect bridge, configuration, initialization, performance and logout: https://github.com/AzureAD/microsoft-authentication-library-for-js/blob/dev/lib/msal-browser/docs/caching.md, https://github.com/AzureAD/microsoft-authentication-library-for-js/blob/dev/lib/msal-browser/docs/token-lifetimes.md, https://github.com/AzureAD/microsoft-authentication-library-for-js/blob/dev/lib/msal-browser/docs/redirect-bridge.md, https://github.com/AzureAD/microsoft-authentication-library-for-js/blob/dev/lib/msal-browser/docs/configuration.md, https://github.com/AzureAD/microsoft-authentication-library-for-js/blob/dev/lib/msal-browser/docs/initialization.md, https://github.com/AzureAD/microsoft-authentication-library-for-js/blob/dev/lib/msal-common/docs/performance.md, https://github.com/AzureAD/microsoft-authentication-library-for-js/blob/dev/lib/msal-browser/docs/logout.md (checked 2026-10-14)
- Refresh tokens and single-page apps without third-party cookies: https://learn.microsoft.com/en-us/entra/identity-platform/refresh-tokens, https://learn.microsoft.com/en-us/entra/identity-platform/reference-third-party-cookies-spas (checked 2026-10-14)
- Hidden-iframe renewal: https://learn.microsoft.com/en-us/entra/identity-platform/msal-js-avoid-page-reloads (checked 2026-10-14)
- Sign-out and SPA redirect URIs: https://learn.microsoft.com/en-us/entra/identity-platform/v2-protocols-oidc, https://learn.microsoft.com/en-us/entra/identity-platform/reply-url (checked 2026-10-14)
- Entra ID error codes: https://learn.microsoft.com/en-us/entra/identity-platform/reference-error-codes (checked 2026-10-14)
- Content Security Policy Level 3 (`frame-src`, `frame-ancestors`): https://www.w3.org/TR/CSP3/ (checked 2026-10-14)
