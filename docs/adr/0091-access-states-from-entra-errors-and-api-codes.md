# ADR 0091: Access states from Entra errors and API codes

- **Status:** Accepted
- **Date:** 2026-10-21
- **Decided by:** Architect
- **Specified in:** 10-web-client.md, 11-api.md, 82-security.md, 84-testing.md, 86-operations.md
- **Related ADRs:** 0012, 0016, 0017, 0018, 0021, 0088, 0089, 0092

## Context

The web client signs users in with MSAL (authorization code with PKCE, redirect flows only, ADR 0018) and calls only the API. Users need a clear state when they are signed out, when they aren't assigned to the app and when they have no projects, and every error must say what to do next and show a reference ID. A sign-in that Entra ID refuses must never leave the user on an endless spinner, and a user with no projects must never reach a dead end. Assignment is required on both the web client and the API, and the API answers a valid token that holds neither `User` nor `Admin` with `403`.

Where the client's signals come from:

- Entra ID returns sign-in failures to the redirect URI; `handleRedirectPromise` raises them as an `AuthError` carrying `errorCode`, `errorNo`, `errorMessage` and `correlationId`.
- Platform authentication answers a missing or invalid token with `401`, and a token whose `azp` isn't an allowed client application (`defaultAuthorizationPolicy.allowedApplications`) or whose `tid` isn't in `WEBSITE_AUTH_AAD_ALLOWED_TENANTS` with `403`; neither response carries a problem body. App Service also answers `403` while the API's site is stopped.
- The API answers every request whose valid token has neither role with `403` and problem code `role_not_assigned`, and a non-admin on an admin route with `403 admin_required`. `getCurrentUser` creates the user's row and default memberships on the first authenticated request (ADR 0021) and returns `isAdmin`; `listMyProjects` returns the reachable projects.

Vendor facts:

- With assignment required, an unassigned user can't sign in or get an access token (`AADSTS50105`). Global Administrators bypass assignment, so one without an API role reaches the API with a token that has no `roles` claim. An assignment-required application can't receive user consent and needs tenant-wide admin consent (`AADSTS65001` no consent, `AADSTS90094` admin consent required).
- Client applications must treat access tokens as opaque strings; their contents are for the API. Access tokens last a random 60 to 90 minutes, so the `roles` claim the API reads changes only when the client renews its token.
- A single-page app's refresh token expires 24 hours after sign-in (`AADSTS700084`); `acquireTokenSilent` then raises `InteractionRequiredAuthError`.

## Decision

1. **Sources of truth.** Every access state comes from exactly four inputs: the `AuthError` that `handleRedirectPromise({ navigateToLoginRequestUrl: false })` raises, the API's problem codes, the bodies of `getCurrentUser` and `listMyProjects`, and the session-storage flag `docchat.signedOut`. The client never decodes an ID or access token, never reads a `roles`, `groups` or `wids` claim and never calls Microsoft Graph; the login request names only the API scope.
2. **Start-up order.** Check `ReadableStream`, `TextDecoder`, `crypto.subtle`, `crypto.randomUUID`, `BroadcastChannel` and `AbortController` before any network call; load `config.json`; create the MSAL instance; await `handleRedirectPromise`; take the redirect result's account, else the first cached account whose `tenantId` equals the configured tenant; then call `getCurrentUser`, and start every other query only after it succeeds. Each state below replaces the start-up spinner; none waits without end.
3. **The states.** The first six replace the whole page; the last two appear inside the app shell.

   | State | Shown when | Actions |
   |---|---|---|
   | Unsupported browser | a required browser API is missing (client outcome `unsupported_browser`) | none |
   | Signed out | no cached account and `docchat.signedOut` is `1`; or `handleRedirectPromise` raises `user_cancelled`, or `access_denied` without `AADSTS50105` | Sign in: remove the flag, then `loginRedirect` |
   | Not assigned | the `AuthError`'s `errorNo` is `"50105"` or its `errorMessage` contains `AADSTS50105`; or any API response is `403` with a problem body whose `code` is `role_not_assigned` (`not_assigned`) | Try again: `loginRedirect` with `prompt: 'select_account'`; Sign out |
   | Consent missing | the `AuthError` contains `AADSTS65001` or `AADSTS90094` (`consent_missing`) | Sign out; the reference ID is the error's `correlationId` |
   | Sign-in failed | any other `AuthError` from `handleRedirectPromise`, or creating the MSAL instance fails (`signin_failed`) | Try again: `loginRedirect`; the reference ID is the error's `correlationId` |
   | Couldn't load your account | `getCurrentUser` fails other than by a `401`, `403 role_not_assigned` or a token that needs interaction | Try again: refetch `getCurrentUser`; reference ID shown |
   | No projects | `listMyProjects` returns no items | a user is asked to contact an admin and gets Check again (refetches `listMyProjects`); an admin is told no projects exist and gets Go to Projects; navigation, the session list and the Memory screen stay available |
   | Admin only | an admin route while `isAdmin` is false, or an admin call answered `403 admin_required` (which first refetches `getCurrentUser`) | Go to chat; the admin code isn't downloaded (ADR 0089) |

4. **Not assigned stops the app.** On `role_not_assigned` every query stops and the shell is replaced. The screen names the signed-in account (MSAL `account.username`), so the user can tell an admin which account to assign. When Entra ID stops an unassigned user on its own sign-in page, the browser never returns to the app; Entra ID's page explains the failure and the app has nothing to detect.
5. **A `403` without a problem body is a server failure.** A non-`2xx` response is a `problem` only when its `Content-Type` is `application/problem+json` and its body validates against the generated `Problem` schema; otherwise it is an `http` failure. A `403` `http` failure shows the server-failure text with the request's trace ID (at start-up on the Couldn't load your account screen, with Try again) and never Not assigned: a missing role reaches the client only as `AADSTS50105` or as `403 role_not_assigned` with a problem body, while a bare `403` comes from platform authentication refusing the token's client application or tenant (a misconfigured deployment) or from App Service while the API's site is stopped. A `401`, with or without a body, takes the one refresh-token retry and then the Session expired state of ADR 0018.
6. **Reference IDs.** Entra ID states show the `AuthError`'s `correlationId`, which Entra ID's sign-in logs hold. API failures show the problem's `correlationId`, or for an `http` failure the trace ID of the request's own `traceparent` (ADR 0088).
7. **Signed out and session expiry.** Sign out sets `docchat.signedOut` to `1` before `logoutRedirect`, so the return to the site root shows Signed out instead of signing in again at once; with no cached account and no flag, the client calls `loginRedirect` immediately (ADR 0018). `InteractionRequiredAuthError`, including `AADSTS700084`, and a second `401` are the Session expired state of ADR 0018, never an access state.
8. **Text keys and outcome codes.** The screens use the text keys `access.unsupported.*`, `access.signedOut.*`, `access.notAssigned.*`, `access.consentMissing.*`, `access.signInFailed.*`, `access.loadUserFailed.*` and `access.adminOnly.*`. The client outcome codes `unsupported_browser`, `signin_failed`, `not_assigned` and `consent_missing` are raised in the browser and are never API codes.

## Alternatives considered

- **Role and projects from token claims.** Rejected: clients must treat access tokens as opaque; a claim changes only at renewal, 60 to 90 minutes later; projects aren't claims at all; and the API's answer is the one that grants or refuses access, so a second source could only disagree with it.
- **Group memberships from Microsoft Graph.** Rejected: it needs a second scope and consent and paged reads, a single page can miss members, and the API would still decide on its own terms.
- **Every `403` shown as Not assigned.** Rejected: a stopped API or a deployment that names the wrong client or tenant would send users to an admin for an assignment that can't help, and would hide an outage behind an access message.
- **A separate access-check operation.** Rejected: `getCurrentUser` already runs first, provisions the user and returns `isAdmin`, and every route answers `role_not_assigned`.
- **Automatic sign-in retries on Not assigned or Consent missing.** Rejected: nothing changes until an administrator acts, so a retry would loop through Entra ID; the screens wait for the user's Try again.
- **Popup sign-in for Try again.** Rejected: the client uses redirect flows only (ADR 0018).

## Consequences

- Every sign-in and access outcome ends on a screen with a next step and, where a request was made, a reference ID; no state spins without end.
- A role change in Entra ID reaches the client when the API sees a renewed token, within the access token's 60-to-90-minute lifetime: a removed `Admin` role shows Admin only at the next admin call, and a removed `User` role shows Not assigned.
- A Global Administrator without an API role sees Not assigned, because the API refuses the token although Entra ID let them sign in.
- A bare `403` whose trace ID matches no request in Application Insights tells operators that platform authentication or a stopped site answered, because the request never reached the app.
- Operations (86-operations.md): Not assigned and Consent missing reports follow the access runbook: assign the user or group to the web client (default access) and to an API role, or grant tenant-wide admin consent for the web client. Sign-in failures are found in Entra ID's sign-in logs by the reference ID.
- Tests (84-testing.md), browser tests against the stub identity provider and stub API (ADR 0092): an authorize error carrying `AADSTS50105` shows Not assigned, `AADSTS65001` shows Consent missing, `access_denied` alone shows Signed out and any other error shows Sign-in failed with the reference ID; `getCurrentUser` answering `403 role_not_assigned` shows Not assigned and no other API call follows; a bare `403` shows Couldn't load your account with the request's trace ID and Try again, never Not assigned; a bare `401` goes through the renewal retry; an empty `listMyProjects` shows the user and admin variants of No projects; a non-admin on an admin route sees Admin only, makes no admin call and downloads no admin module. Deployed: the `dev` no-role test user gets `403 role_not_assigned` from `getCurrentUser`.

## References

- Microsoft Entra error codes (`AADSTS50105`, `AADSTS65001`, `AADSTS90094`, `AADSTS700084`): https://learn.microsoft.com/en-us/entra/identity-platform/reference-error-codes (checked 2026-10-14)
- Restrict an app to a set of users (assignment required, Global Administrator bypass, admin consent): https://learn.microsoft.com/en-us/entra/identity-platform/howto-restrict-your-app-to-a-set-of-users (checked 2026-10-14)
- Access tokens (opaque to clients; 60 to 90 minutes): https://learn.microsoft.com/en-us/entra/identity-platform/access-tokens (checked 2026-10-21)
- Refresh tokens (24-hour single-page-app lifetime): https://learn.microsoft.com/en-us/entra/identity-platform/refresh-tokens (checked 2026-10-14)
- App Service authentication with Microsoft Entra ID (allowed applications, allowed tenants, `403` on a failed check): https://learn.microsoft.com/en-us/azure/app-service/configure-authentication-provider-aad (checked 2026-10-14)
- MSAL.js initialization and `handleRedirectPromise`: https://github.com/AzureAD/microsoft-authentication-library-for-js/blob/dev/lib/msal-browser/docs/initialization.md (checked 2026-10-14)
- MSAL.js token lifetimes and `InteractionRequiredAuthError`: https://github.com/AzureAD/microsoft-authentication-library-for-js/blob/dev/lib/msal-browser/docs/token-lifetimes.md (checked 2026-10-14)
- Assign users and groups to an enterprise application: https://learn.microsoft.com/en-us/entra/identity/enterprise-apps/assign-user-or-group-access-portal (checked 2026-10-14)
- RFC 9457, Problem Details for HTTP APIs: https://www.rfc-editor.org/rfc/rfc9457 (checked 2026-10-14)
