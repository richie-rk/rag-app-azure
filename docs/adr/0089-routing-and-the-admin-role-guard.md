# ADR 0089: Routing and the admin role guard

- **Status:** Accepted
- **Date:** 2026-10-21
- **Decided by:** Architect
- **Specified in:** 10-web-client.md, 82-security.md, 84-testing.md
- **Related ADRs:** 0004, 0017, 0018, 0020, 0021, 0082, 0083, 0086, 0090, 0091, 0092, 0133

## Context

- The `Admin` and `User` app roles live in Entra ID. The API checks the `Admin` role on every admin route and answers `403` to a non-admin; admins act on every project without membership rows; the API creates the user's row and default memberships on the first authenticated request. Authorisation is decided only on the server: the browser decides nothing about access.
- Admin screens are hidden from non-admins and refuse to load for them, and Static Web Apps falls back to the app shell for in-app paths, so every screen has a real URL.
- Vendor facts (checked 2026-10-14): React Router's data mode (`createBrowserRouter`, `RouterProvider`) routes on browser-history paths; Static Web Apps rewrites navigations that match no file to the `navigationFallback` target, except paths in `exclude`, and route rules don't apply to responses that trigger the fallback.

## Decision

1. **Router.** React Router 8.4.0 in data mode (`createBrowserRouter`, `RouterProvider`) with browser-history paths, never hash routing.
2. **Routes**, each with its own document title of the form `<screen> · DocChat`:

   | Path | Screen | Guard |
   |---|---|---|
   | `/` | Start-up, then replaced by `/chat` | none |
   | `/chat` | Chat, new conversation; optional `?project=<uuid>` | signed in |
   | `/chat/:sessionId` | Chat, existing session; a `sessionId` that isn't a canonical UUID is replaced by `/chat` | signed in |
   | `/memory` | Memory | signed in |
   | `/admin/projects` | Projects; `?q=`, `?status=active\|archived\|all` | `Admin` |
   | `/admin/projects/new` | New project | `Admin` |
   | `/admin/projects/:projectId` | Project; `?tab=details\|members\|indexes` | `Admin` |
   | `/admin/indexes` | Indexes | `Admin` |
   | `/admin/indexes/new` | New index | `Admin` |
   | `/admin/indexes/:indexId` | Index | `Admin` |
   | `/admin/indexes/:indexId/documents` | Documents; `?q=`, `?state=`, `?document=<uuid>` | `Admin` |
   | `/admin/runs` | Ingestion runs; `?indexId=`, `?state=` | `Admin` |
   | `/admin/runs/:runId` | Ingestion run | `Admin` |
   | any other path | replaced by `/chat` | none |

3. **The fallback.** Static Web Apps serves `index.html` with `200` for every navigation to a path that isn't a file, so every route works on reload and as a shared link; the excluded patterns (`/assets/*`, `/.auth/*`, `/config.json`, `/favicon.svg`, `/not-found.html` and files with the listed extensions) answer `404` with `not-found.html` when missing (ADR 0083).
4. **Role and projects from the API.** `getCurrentUser` is the first API call after sign-in, and every other query waits for it, because the API provisions the user on that first request. Admin status is its `isAdmin`; reachable projects come only from `listMyProjects`. The client never decodes token claims.
5. **The admin guard.** Routes under `/admin/` render only when `isAdmin` is true; otherwise the Admin only screen replaces the main content, with a way back to chat. The admin screens and the memory screen are `React.lazy` modules loaded inside their guard, so a non-admin's browser never downloads admin code, and admin navigation items are hidden for non-admins. When an admin call answers `403 admin_required` (the role was removed during the session), the client refetches `getCurrentUser` and shows Admin only.
6. **Only the API protects data.** The guard is presentation: the API checks the `Admin` role from the token on every admin operation and answers `403` to anyone without it, and admins see every project without membership rows.
7. **The return path after sign-in.** Each interactive sign-in carries `state` = the in-app path (`pathname` plus `search`); after the redirect the client replaces the location with it only when it starts with exactly one `/`, contains no `\`, is at most 512 characters long and resolves to the app's own origin, and otherwise with `/chat`.
8. **Focus on navigation.** On every route change, focus moves to the screen's `h1` (`tabindex="-1"`).
9. **A release under an open tab.** When a lazy module fails to load because its hashed file left with the previous release (an excluded path answers `404`, not HTML), the router's error element shows the app-updated screen with Reload; any other rendering error shows the something-went-wrong screen with Reload. Neither shows a reference ID, because no request failed.

## Alternatives considered

- **Hash routing.** Rejected: real paths give readable, shareable links, and the navigation fallback makes them work with no server code.
- **Reading the role from the ID token in the browser.** Rejected: the API is the authority and provisions users on the first call; one source keeps the shell and the API in agreement, and a role change shows at the next `getCurrentUser`.
- **A separate admin site.** Rejected: a second registration, deployment and origin, while code splitting already keeps admin code out of non-admin browsers.
- **A not-found screen for unknown paths.** Rejected: a stale or mistyped link lands on the screen most people want.
- **Loading admin modules for everyone.** Rejected: it ships code that non-admins never use and enlarges the initial bundle, which the build gate caps at 500 KB gzip-compressed (ADR 0086).

## Consequences

- Non-admins never see or download admin screens; the guard is a convenience, and the API's authorisation matrix proves `403` for non-admins on every admin route (ADR 0133).
- Every route survives a reload and works as a link; unknown paths land on chat.
- After a deploy, an open tab that navigates to a lazy route whose file changed shows the app-updated screen and reloads into the new release.
- Obligations (84-testing.md): browser tests that a non-admin opening `/admin/projects` sees Admin only, makes no admin API call and downloads no admin module; that `/chat/<uuid>` serves the shell with `200`; that with no account `/memory` redirects to the authorize endpoint with `state=/memory` and returns to the Memory screen; that a tampered `state` of `//evil.example` or `https://evil.example/` lands on `/chat`; and that `403 admin_required` refetches `getCurrentUser` and shows Admin only.

## References

- React Router modes (data mode): https://reactrouter.com/start/modes (checked 2026-10-14)
- Static Web Apps configuration (navigation fallback, exclusions, route rules): https://learn.microsoft.com/en-us/azure/static-web-apps/configuration (checked 2026-10-14)
