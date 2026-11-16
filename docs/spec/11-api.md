# 11 — API

This file specifies the DocChat API's HTTP interface: the conventions every route follows (authentication, the order of checks, authorisation, problem details, correlation IDs, validation, paging, optimistic concurrency, headers, request limits, idempotency and the OpenAPI document), the catalogue of error codes, each of the 45 operations with its parameters, request and response models, status codes, error codes, authorisation rule, limits and concurrency behaviour, and the schemas that several operations share. One FastAPI application serves every operation (ADR 0003), and the browser calls it directly with bearer tokens (ADR 0004). The algorithms behind the operations live in `12-chat-pipeline.md` (chat turns, limits and citations), `13-conversations-and-memory.md` (sessions, memory and feedback) and `14-ingestion.md` (indexes, documents, uploads and runs); tables and documents are in `80-data-model.md`, settings in `81-config.md`, telemetry in `85-observability.md` and the tests that prove each rule in `84-testing.md`.

## 1. Conventions

### 1.1 Application, paths, versioning and pinned versions

- **Application.** The API is the package `docchat_api` (`apps/api`). Its composition root `docchat_api.app:create_app()` builds one FastAPI application with `openapi_url=None`, `docs_url=None`, `redoc_url=None` and `redirect_slashes=False`, so no environment serves the OpenAPI document or a documentation page (`GET /openapi.json`, `/docs` and `/redoc` answer `404 route_not_found`) and a trailing slash is never redirected (`GET /v1/me/` answers `404 route_not_found`), because a redirect breaks CORS preflights. `create_app()` takes the two request limiters as ports (section 1.11) and builds without settings or network access; settings, Azure clients and the first ops-config snapshot load in the lifespan start-up, and the process exits before serving when any of them fails.
- **Paths.** `GET /healthz` is the only route outside `/v1`. Every other route is under `/v1`, every administrator route under `/v1/admin`. Every `/v1` route is mounted on one router, `APIRouter(route_class=DocChatRoute)` with the route class `docchat_api.http.routing.DocChatRoute`, and takes the caller from the dependency `current_principal`; no area declares an authentication dependency of its own (section 1.3).
- **Methods.** Each path answers exactly the methods of the table below. A known path called with another method answers `405 method_not_allowed` with an `Allow` header naming the path's methods. No route answers `HEAD` or `OPTIONS`; CORS preflights are answered by App Service platform CORS before the app (section 1.10).
- **Versioning.** `/v1` is the only version. A change inside `/v1` adds response fields, adds optional request fields or adds error codes, and never removes, renames or retypes a field, a parameter, an operation or a code. A breaking change ships as `/v2` beside `/v1` and `/v1` stays until the web client has moved. Clients ignore unknown response fields (the web client's generated schemas strip them, `10-web-client.md` section 5), and a new value of a response enumeration reaches the web client before the API returns it (section 1.13).

**The operations.** Access, tags and the write limit are defined in sections 1.4, 1.13 and 1.11; "W" marks the 17 write-limited operations.

| # | Method and path | operationId | Tag | Access | W | `If-Match` | Section |
|---|---|---|---|---|---|---|---|
| 1 | `GET /healthz` | `getHealth` | `health` | `public` | – | – | 3.1.1 |
| 2 | `GET /v1/me` | `getCurrentUser` | `me` | `user` | – | – | 3.2.1 |
| 3 | `GET /v1/projects` | `listMyProjects` | `projects` | `user` | – | – | 3.2.2 |
| 4 | `POST /v1/chat/turns` | `createChatTurn` | `chat` | `user` | – | – | 3.3 |
| 5 | `GET /v1/sessions` | `listSessions` | `sessions` | `user` | – | – | 3.4.1 |
| 6 | `GET /v1/sessions/{sessionId}` | `getSession` | `sessions` | `user` | – | – | 3.4.2 |
| 7 | `GET /v1/sessions/{sessionId}/turns` | `listSessionTurns` | `sessions` | `user` | – | – | 3.4.3 |
| 8 | `PUT /v1/sessions/{sessionId}/turns/{turnId}/feedback` | `putTurnFeedback` | `sessions` | `user` | W | – | 3.4.4 |
| 9 | `GET /v1/sessions/{sessionId}/turns/{turnId}/citations/{citationNumber}` | `getCitation` | `chat` | `user` | – | – | 3.5.1 |
| 10 | `GET /v1/sessions/{sessionId}/turns/{turnId}/citations/{citationNumber}/content` | `getCitationContent` | `chat` | `user` | – | – | 3.5.2 |
| 11 | `GET /v1/sessions/{sessionId}/turns/{turnId}/citations/{citationNumber}/document` | `getCitationDocument` | `chat` | `user` | – | – | 3.5.3 |
| 12 | `GET /v1/memory` | `getMemory` | `memory` | `user` | – | – | 3.6.1 |
| 13 | `PATCH /v1/memory` | `updateMemorySettings` | `memory` | `user` | W | memory `etag` | 3.6.2 |
| 14 | `PUT /v1/memory/facts/{factId}` | `updateMemoryFact` | `memory` | `user` | W | memory `etag` | 3.6.3 |
| 15 | `DELETE /v1/memory/facts/{factId}` | `deleteMemoryFact` | `memory` | `user` | W | memory `etag` | 3.6.4 |
| 16 | `DELETE /v1/memory/facts` | `clearMemory` | `memory` | `user` | W | memory `etag` | 3.6.5 |
| 17 | `GET /v1/admin/projects` | `adminListProjects` | `admin-projects` | `admin` | – | – | 3.7.2 |
| 18 | `POST /v1/admin/projects` | `adminCreateProject` | `admin-projects` | `admin` | W | – | 3.7.3 |
| 19 | `GET /v1/admin/projects/{projectId}` | `adminGetProject` | `admin-projects` | `admin` | – | – | 3.7.4 |
| 20 | `PATCH /v1/admin/projects/{projectId}` | `adminUpdateProject` | `admin-projects` | `admin` | W | project `rowVersion` | 3.7.5 |
| 21 | `GET /v1/admin/projects/{projectId}/members` | `adminListProjectMembers` | `admin-projects` | `admin` | – | – | 3.7.6 |
| 22 | `PUT /v1/admin/projects/{projectId}/members/{userId}` | `adminAddProjectMember` | `admin-projects` | `admin` | W | project `rowVersion` | 3.7.7 |
| 23 | `DELETE /v1/admin/projects/{projectId}/members/{userId}` | `adminRemoveProjectMember` | `admin-projects` | `admin` | W | project `rowVersion` | 3.7.8 |
| 24 | `GET /v1/admin/users` | `adminSearchUsers` | `admin-users` | `admin` | – | – | 3.8.1 |
| 25 | `GET /v1/admin/projects/{projectId}/indexes` | `adminListProjectIndexes` | `admin-projects` | `admin` | – | – | 3.9.1 |
| 26 | `PUT /v1/admin/projects/{projectId}/indexes/{indexId}` | `adminMapProjectIndex` | `admin-projects` | `admin` | W | project `rowVersion` | 3.9.2 |
| 27 | `DELETE /v1/admin/projects/{projectId}/indexes/{indexId}` | `adminUnmapProjectIndex` | `admin-projects` | `admin` | W | project `rowVersion` | 3.9.3 |
| 28 | `GET /v1/admin/index-options` | `adminGetIndexOptions` | `admin-indexes` | `admin` | – | – | 3.10.1 |
| 29 | `GET /v1/admin/indexes` | `adminListIndexes` | `admin-indexes` | `admin` | – | – | 3.10.2 |
| 30 | `POST /v1/admin/indexes` | `adminCreateIndex` | `admin-indexes` | `admin` | W | – | 3.10.3 |
| 31 | `GET /v1/admin/indexes/{indexId}` | `adminGetIndex` | `admin-indexes` | `admin` | – | – | 3.10.4 |
| 32 | `PATCH /v1/admin/indexes/{indexId}` | `adminUpdateIndex` | `admin-indexes` | `admin` | W | index `rowVersion` | 3.10.5 |
| 33 | `DELETE /v1/admin/indexes/{indexId}` | `adminDeleteIndex` | `admin-indexes` | `admin` | W | index `rowVersion` | 3.10.6 |
| 34 | `POST /v1/admin/indexes/{indexId}/reingest` | `adminStartReingest` | `admin-indexes` | `admin` | W | index `rowVersion` | 3.10.7 |
| 35 | `GET /v1/admin/indexes/{indexId}/documents` | `adminListDocuments` | `admin-documents` | `admin` | – | – | 3.11.1 |
| 36 | `GET /v1/admin/indexes/{indexId}/documents/{documentId}` | `adminGetDocument` | `admin-documents` | `admin` | – | – | 3.11.2 |
| 37 | `GET /v1/admin/indexes/{indexId}/documents/{documentId}/pdf` | `adminGetDocumentPdf` | `admin-documents` | `admin` | – | – | 3.11.10 |
| 38 | `DELETE /v1/admin/indexes/{indexId}/documents/{documentId}` | `adminDeleteDocument` | `admin-documents` | `admin` | W | document `rowVersion` | 3.11.3 |
| 39 | `POST /v1/admin/indexes/{indexId}/documents/{documentId}/reingest` | `adminReingestDocument` | `admin-documents` | `admin` | W | document `rowVersion` | 3.11.4 |
| 40 | `POST /v1/admin/indexes/{indexId}/uploads/preflight` | `adminPreflightUploads` | `admin-uploads` | `admin` | – | – | 3.11.6 |
| 41 | `POST /v1/admin/indexes/{indexId}/uploads` | `adminReserveUpload` | `admin-uploads` | `admin` | – | – | 3.11.7 |
| 42 | `PUT /v1/admin/indexes/{indexId}/uploads/{versionId}/content` | `adminUploadContent` | `admin-uploads` | `admin` | – | – | 3.11.8 |
| 43 | `POST /v1/admin/indexes/{indexId}/uploads/submit` | `adminSubmitUploads` | `admin-uploads` | `admin` | – | – | 3.11.9 |
| 44 | `GET /v1/admin/ingestion-runs` | `adminListIngestionRuns` | `admin-runs` | `admin` | – | – | 3.12.1 |
| 45 | `GET /v1/admin/ingestion-runs/{runId}` | `adminGetIngestionRun` | `admin-runs` | `admin` | – | – | 3.12.2 |

**Pinned versions** (each confirmed on its official package page on 2026-10-14; the lockfile of `01-repository.md` section 3 pins them exactly):

| Component | Version | Used for |
|---|---|---|
| FastAPI | 0.143.1 | the application, routing, validation |
| Pydantic | 2.14.1 | every request, response and settings model |
| pydantic-settings | 2.16.0 | `AuthSettings`, `CoreSettings`, `ApiSettings` |
| Gunicorn | 26.2.0 | the process manager (section 1.15) |
| Uvicorn | 0.54.0 | the ASGI server, subclassed by `DocChatServer` |
| uvicorn-worker | 0.4.0 | `uvicorn_worker.UvicornWorker`, subclassed by `DocChatUvicornWorker` |
| PyJWT, installed as `pyjwt[crypto]` | 2.15.1 | token validation |
| cryptography | 50.0.2 | RS256 verification under PyJWT |
| httpx | 0.28.1 | the signing-key fetch and the Prompt Shields calls (the only first-party HTTP client, ADR 0130) |
| azure-cosmos | 4.17.1 | the write limit's `usage` documents; the conversation and memory stores |
| azure-storage-blob | 12.31.0 | uploads and citation files |
| azure-storage-queue | 12.18.0 | the ingestion queue message after a run is created |
| OpenAPI | 3.1 | the exported document (section 1.13) |
| `@hey-api/openapi-ts` | 0.99.0 | the web client's types and Zod schemas generated from the document (`10-web-client.md` section 5) |

### 1.2 Authentication

Every route except `GET /healthz` requires a Microsoft Entra ID access token for the API, sent as `Authorization: Bearer <token>`. Two layers check it (ADR 0017).

**Layer 1: platform authentication.** In every deployed environment App Service authentication (`authsettingsV2`, configured in Bicep as `83-infrastructure.md` specifies) runs before the app. It answers a request without a valid token for the API with `401` and `WWW-Authenticate: Bearer`, never with a redirect, and a token whose authorised party or tenant isn't allowed with `403`. These answers come from the platform and carry no problem body; the web client classifies them by status (section 1.5). Platform authentication excludes no path, and App Service's health check passes through it. A local process and the test suites run without it, which is why the app validates every token itself and behaves the same with and without the platform layer.

**Layer 2: in-app validation.** `docchat_core.auth.TokenValidator.validate(authorization_header) -> Principal` runs on every request except `GET /healthz`, before any other work of the request (section 1.3). Its steps, the first failure winning:

| Step | Check | Failure | Telemetry `reason` |
|---|---|---|---|
| 1 | An `Authorization` header with scheme `Bearer` (case-insensitive) | `401 authentication_required` | `missing_header`, `bad_scheme` |
| 1 | Exactly one `Authorization` header; one token of 1 to 16,384 characters in three dot-separated parts | `401 invalid_token` | `duplicate_header`, `token_too_long`, `malformed` |
| 2 | The unverified JOSE header: `alg` exactly `RS256`; `kid` a string of 1 to 128 characters; `typ` absent or `JWT` | `401 invalid_token` | `bad_alg`, `missing_kid`, `malformed` |
| 3 | The public key for `kid` from the signing-key provider (below) | `401 invalid_token` when the `kid` is still unknown after the on-demand refresh; `503 authentication_unavailable` (`retryAfterSeconds` 30) when no valid key set exists and the fetch failed | `unknown_kid`, `keys_unavailable` |
| 4 | `jwt.decode(token, key, algorithms=["RS256"], audience=DOCCHAT_AUTH_API_CLIENT_ID, issuer="https://login.microsoftonline.com/<DOCCHAT_AUTH_TENANT_ID>/v2.0", leeway=60, options={"require": ["exp", "nbf", "iat", "iss", "aud", "sub", "oid", "tid", "azp", "scp", "ver"], "strict_aud": True})` with every `verify_*` option true | `401 invalid_token` | `InvalidSignatureError` → `bad_signature`; `ExpiredSignatureError` → `expired`; `ImmatureSignatureError` → `not_yet_valid`; `InvalidIssuedAtError` → `bad_iat`; `InvalidIssuerError` → `bad_issuer`; `InvalidAudienceError` → `bad_audience`; `MissingRequiredClaimError` → `missing_claim`; `InvalidAlgorithmError` → `bad_alg`; any other `DecodeError` → `malformed` |
| 5 | `ver` is `2.0` | `401 invalid_token` | `bad_version` |
| 6 | `tid` equals `DOCCHAT_AUTH_TENANT_ID` | `401 invalid_token` | `bad_tenant` |
| 7 | `azp` is in the allowed client set (below) | `401 invalid_token` | `bad_client` |
| 8 | `scp` is a string whose space-separated values include `access_as_user` | `401 invalid_token` | `missing_scope` |
| 9 | `oid` parses as a UUID | `401 invalid_token` | `bad_oid` |
| 10 | `roles` is absent or an array of strings | `401 invalid_token` | `bad_roles` |
| 10 | `roles` contains `User` or `Admin` | `403 role_not_assigned` | `no_role` |

Rules of the validator:

- Only RS256 is accepted: tokens with `alg` `none`, `HS*`, `PS*` or `ES*` fail at step 2, before any key lookup. Decoding is CPU work of well under 1 ms and runs on the event loop.
- Every in-app `401` carries `WWW-Authenticate: Bearer realm="docchat"`; `401 invalid_token` carries `WWW-Authenticate: Bearer realm="docchat", error="invalid_token"`. A token for another tenant, client or scope is `401` in the app, because it is no DocChat user token at all; `403` is kept for authenticated callers without permission (section 1.4).
- Nothing from a failed token (its claims, its header, the token) is logged; only the `reason`, the `azp` when step 4 passed, and the outcome.
- **Allowed client set:** `{DOCCHAT_AUTH_WEB_CLIENT_ID}`, plus `DOCCHAT_AUTH_TEST_CLIENT_ID` when that setting is non-empty and `DOCCHAT_CORE_ENVIRONMENT` is `dev`, `load` or `local`. Settings validation at start-up refuses to start the process when `DOCCHAT_CORE_ENVIRONMENT` is `prod` and `DOCCHAT_AUTH_TEST_CLIENT_ID` is non-empty, when the API, web and test client IDs aren't distinct, and when `DOCCHAT_CORE_ENVIRONMENT` is `local` while `WEBSITE_SITE_NAME` or `WEBSITE_INSTANCE_ID` is set (both are set by App Service and Functions, so `local` never runs in Azure).
- **Identity comes only from the validated claims.** The API never reads `X-MS-CLIENT-PRINCIPAL`, `X-MS-CLIENT-PRINCIPAL-ID`, `X-MS-CLIENT-PRINCIPAL-NAME`, `X-MS-CLIENT-PRINCIPAL-IDP`, `X-MS-TOKEN-*` or any other `X-MS-*` header, and never takes a user identifier from a path, query, body or cursor to decide who the caller is. A unit test fails when the strings `x-ms-client-principal` or `x-ms-token` (case-insensitive) appear in `apps/api` or `packages/core` outside tests.

**The `Principal` model** (`docchat_core.auth.Principal`, a frozen Pydantic model built only by `TokenValidator`; the raw token is never kept):

| Field | Type | Source and rule |
|---|---|---|
| `oid` | UUID | the `oid` claim; its lower-case hyphenated text is every Cosmos DB partition key value of the caller (section 1.4.2) |
| `tid` | UUID | the `tid` claim |
| `azp` | UUID | the `azp` claim |
| `client` | `web` or `test` | from `azp` |
| `roles` | frozen set of `Admin` and `User` | the `roles` claim; any other value is dropped |
| `is_admin` | boolean | `Admin` in `roles` |
| `display_name` | string, 0 to 256 code points | the `name` claim: Unicode NFC, then every code point of category Cc, Cf, Cs, Co or Cn removed, trimmed, cut to 256; `""` when absent |
| `email` | string, 0 to 320 code points | the `email` claim; else `preferred_username` when it holds exactly one `@`, no whitespace and no `#`; trimmed; else `""` |

`display_name` and `email` are display data for the caller's `app.users` row and `getCurrentUser` only; no decision uses them and no log carries them.

**Signing keys.** `docchat_core.auth.EntraSigningKeyProvider` implements the protocol `docchat_core.auth.SigningKeyProvider`; tests inject a static key set through the same protocol, and no setting changes the key source.

- It fetches `https://login.microsoftonline.com/<DOCCHAT_AUTH_TENANT_ID>/discovery/v2.0/keys`, the URL built only from that setting, with one `httpx.AsyncClient` created at start-up and closed at shutdown: `timeout=httpx.Timeout(8.0, connect=4.0)`, `follow_redirects=False`, `limits=httpx.Limits(max_connections=4)`, TLS verification on. Each fetch, connection and body read included, runs inside `asyncio.timeout(8.0)`.
- A response is used only when it is `200`, at most 1 MiB, and a JSON object with a `keys` array. Each key with `kty` `RSA`, a `kid`, and `use` absent or `sig` becomes a public key through `jwt.PyJWK(key, algorithm="RS256")`; a key that fails to parse is skipped; the first 100 keys are kept; a response with no usable key counts as a failed fetch.

```text
state: key_set (kid -> public key) or none; fetched_at; last_on_demand_at; lock (one asyncio.Lock)
valid() = key_set is not none and now - fetched_at < 24 h

get_key(kid):
    if valid() and kid in key_set: return key_set[kid]
    gap = 300 s if valid() else 30 s
    if last_on_demand_at is none or now - last_on_demand_at >= gap:
        await refresh(on_demand=true, gap)
    if valid() and kid in key_set: return key_set[kid]
    if not valid(): raise AuthenticationUnavailable        # 503 authentication_unavailable, retryAfterSeconds 30
    raise InvalidToken("unknown_kid")                      # 401 invalid_token

refresh(on_demand, gap):
    async with lock:                                       # single flight: concurrent callers await one fetch
        if on_demand and last_on_demand_at is not none and now - last_on_demand_at < gap: return
        if on_demand: last_on_demand_at = now
        try:
            async with asyncio.timeout(8.0):
                response = await http.get(JWKS_URI)
            require status 200, body <= 1 MiB, JSON object with array "keys"
            parsed = usable RSA keys as above; require at least 1; keep the first 100
            key_set = parsed; fetched_at = now
        except any error:
            keep key_set; log warning signing_keys_fetch_failed (status code, error type)

background task (started at start-up without being awaited, cancelled at shutdown):
    refresh(on_demand=false); then every 3,600 s refresh(on_demand=false)
```

A key set stays valid for 24 hours after the fetch that produced it, so a failed hourly refresh keeps serving until the set is 24 hours old.

### 1.3 The order of checks on every route

**Request pipeline.** Every request passes these stages in order:

1. **ASGI middleware, outermost first.** (a) The Azure Monitor OpenTelemetry distro's FastAPI instrumentation starts the server span and continues an incoming valid `traceparent` (section 1.6). (b) The security-header middleware adds the headers of section 1.10 to every response the app sends. (c) The request-size and content-type guard wraps `receive` and acts at the first read of the body: it takes the matched route's `route_options` from the ASGI scope (placed there by `DocChatRoute`), refuses a `Content-Type` outside the route's `content_types` with `415 unsupported_media_type`, compares `Content-Length` with the route's `body_limit` before the first read, and counts bytes as they arrive; either size breach raises `RequestTooLarge`, answered `413 request_too_large` (section 1.7).
2. **Routing.** An unknown path answers `404 route_not_found`; a known path with another method answers `405 method_not_allowed` with `Allow`; `GET /healthz` goes straight to its handler. In a deployed environment platform authentication answers an anonymous request first, so an anonymous call to an unknown path gets the platform's `401`.
3. **Pre-body checks.** `DocChatRoute` runs these checks in its route handler before FastAPI reads the body or resolves the endpoint's dependencies, because FastAPI reads and parses a body before it resolves dependencies, and checks written as dependencies would let `413`, `415` or `422` answer before `401` and would parse the bodies of refused requests: (i) token validation and the role check (section 1.2), which build the `Principal` and set the span attributes `enduser.id` (the caller's `oid`) and `docchat.auth.client`; (ii) the per-user burst limit, unless the route declares `burst_limited=False`; (iii) `require_admin` on every `/v1/admin` route; (iv) the per-user write limit when the route declares `write_limited=True`; (v) first-sign-in provisioning `docchat_core.users.ensure_user` (section 3.2.1). The `Principal` is stored in `request.state.principal`; endpoints receive it through `current_principal`, which only reads it.
4. **Validation** of path, query, headers and body by FastAPI and Pydantic (section 1.7).
5. **Handler:** the access functions of section 1.4.3, then the operation's own lookups, preconditions, state rules and statements.
6. **Exception handlers.** Each `DocChatProblem` subclass carries its `code` and `status` and becomes that code's problem; `RequestValidationError` becomes `422 validation_failed` (or the operation's own `422` code where section 3 says so); any other exception becomes `500 internal_error`, logged once at error level as the record `request_failed` with `error.type` (the qualified name `module.QualName` of the exception's class) and `docchat.error_code` (`internal_error`) and never the exception's message, because messages can carry SQL parameters, document text or identifiers. The server span records the failure through `record_failure(span, exc, "internal_error")`, whose `exception` event keeps only frames and type names (`85-observability.md` section 3).

**The order of checks.** Every route except `createChatTurn` evaluates these steps in order; the first failure wins:

| Step | Check | Failure |
|---|---|---|
| 1 | Platform authentication (deployed environments) | the platform's `401` or `403`, no problem body |
| 2 | In-app token validation (section 1.2) | `401 authentication_required`, `401 invalid_token`, `503 authentication_unavailable` |
| 3 | The `User` or `Admin` role | `403 role_not_assigned` |
| 3a | Per-user burst limit: every `/v1` route except `createChatTurn`; touches no store | `429 too_many_requests` |
| 4 | The `Admin` role on every `/v1/admin` route, before any validation or lookup | `403 admin_required` |
| 4a | Per-user write limit: the 17 write-limited operations | `429 write_rate_limited`; `503 dependency_unavailable` when the check can't reach Cosmos DB |
| 5 | First-sign-in provisioning | `503 dependency_unavailable` |
| 6 | Body size and media type, checked when the body is first read | `413 request_too_large`, `415 unsupported_media_type` |
| 7 | Path, query, header and body validation against the operation's models | `422 validation_failed` |
| 8 | Lookup and reachability of every identifier | `404 <resource>_not_found` |
| 9 | Preconditions | `428 precondition_required`, then `412 precondition_failed` |
| 10 | State rules | `409 <conflict code>` |

Consequences: a refused request's body is never read or parsed; a non-admin learns nothing about an administrator route, not even its body schema; steps 3a and 4a run before provisioning and before the body is read, so a refused request costs no SQL write; and a `404` never depends on whether the resource exists in a project the caller can't reach. An operation's own `422` codes that need a lookup or a setting (for example `422 batch_too_large`, `422 project_not_mapped`, `422 method_not_found`) run in the handler at the position its section in 3 states. Section 3 gives each operation's order where it has more than these steps.

**`createChatTurn`.** The route declares `burst_limited=False` and `write_limited=False`, so steps 3a and 4a don't run; its own limits replace them (ADR 0058). After step 5 it runs the chat pre-stream order (ADR 0055; the algorithms are `12-chat-pipeline.md` section 2):

| Chat step | Check | Failure |
|---|---|---|
| 1 | Steps 1 to 5 above, without 3a and 4a | `401`, `403`, `503` |
| 2 | Body size (32,768 bytes), media type, then `ChatTurnRequest` validation | `413 request_too_large`, `415 unsupported_media_type`, `422 validation_failed` |
| 3 | Request-rate check and count | `429 rate_limited`; `503 dependency_unavailable` |
| 4 | Token quota check | `429 token_quota_exceeded`; `503 dependency_unavailable` |
| 5 | Stream lease | `429 too_many_streams`; `503 dependency_unavailable` |
| 6 | Project access (`authorize_project_for_chat`) | `404 project_not_found`; `503 dependency_unavailable` |
| 7 | Index resolution (`resolve_chat_index`) | `409 project_index_unavailable`; `503 dependency_unavailable` |
| 8 | Session load, duplicate detection and turn creation (`ConversationStore.begin_turn`) | `404 session_not_found`; `409 turn_in_progress`, `409 turn_already_ended`, `409 client_turn_id_conflict`, `409 session_turn_limit`; `503 dependency_unavailable`; or a replay of a completed turn |
| 9 | The turn task starts and the `200` stream begins | every later failure is an `error` event (section 3.3.3) |

A lease acquired at chat step 5 is released before any later refusal is answered.

**`getCitation`, `getCitationContent` and `getCitationDocument`.** After steps 1 to 5: path validation (`422`); for `getCitationContent` and `getCitationDocument`, the download request window (`429 rate_limited`) and the download lease (`429 too_many_downloads`), both failing closed with `503 dependency_unavailable`; then the turn read, the project check, the stored citation, the document check and the rendition check (`404 citation_not_found`, `410 document_changed`); for `getCitationDocument`, the whole-document check (`409 document_pdf_unavailable`); then, for the two file operations, the blob (`500 citation_content_missing`). Every refusal after the lease is acquired releases the lease (section 3.5). `adminGetDocumentPdf` runs the same window and lease after the administrator check and path validation, then its index and document lookups (section 3.11.10).

**The four upload operations.** After steps 1 to 7 (the write limit doesn't apply to them): the index lookup (`404 index_not_found`), the malware-scanning gate (`503 malware_scanning_not_ready`), the index status (`409 index_not_accepting_uploads`), then the operation's own checks (section 3.11.5). `adminUploadContent` has no body model, so its media type and size checks happen at its first body read, after its pre-read checks (section 3.11.8).

### 1.4 Authorisation

#### 1.4.1 Callers and access levels

Each operation carries one access level, published as `x-docchat-access` (section 1.13):

| Access level | Allowed callers | Operations |
|---|---|---|
| `public` | anyone; no in-app check of any kind | `getHealth` only |
| `user` | a token holding `User` or `Admin` | the 14 operations under `/v1` outside `/v1/admin` |
| `admin` | a token holding `Admin` | the 28 operations under `/v1/admin` |

| Caller | What the API lets them do |
|---|---|
| `User` | Chat in, and open citations from, the active projects they are a member of; read and manage their own sessions, feedback and memory |
| `Admin` | Everything a `User` can, in every active project, without membership rows; manage projects, members, index mappings, indexes and documents; see ingestion runs |
| Operator | Nothing through the API: operators change ops config and per-user index overrides only with `docchat-ops` and read conversation data only through just-in-time access (`86-operations.md` section 2) |

#### 1.4.2 Rules

1. **Deny by default, in one module.** `docchat_core.access` answers every access question for every route and data path. Each of its functions returns a positive result or raises; none returns a boolean a caller could ignore, and none accepts a user identifier other than the validated `Principal` (ADR 0020).
2. **Project reachability.** A project is reachable by a caller when its row exists, its `status` is `active`, and the caller holds `Admin` or has an `app.project_members` row for it. On every user path (chat, citations, session lists and reads, feedback and the project picker) an unreachable project behaves exactly as an absent one. Archived projects are unreachable on those paths for everyone, administrators included; administrator routes list, read and restore them (ADR 0022).
3. **Ownership through partition keys.** Every Cosmos DB read, write and query in the containers `conversations`, `memories` and `usage` uses `str(principal.oid)` (lower-case hyphenated) as the user-level partition key value. No request field, path segment, header or cursor supplies it, no operation takes another user's ID for these containers (administrators included), and a session, turn or fact ID from a request addresses documents only inside the caller's own partition, where another user's ID simply doesn't exist.
4. **Sessions, turns and citations follow project reachability.** `getSession`, `listSessionTurns`, `putTurnFeedback`, `getCitation`, `getCitationContent` and `getCitationDocument` call `authorize_project_for_chat` with the stored `projectId` and turn a refusal into their own `404` code (`session_not_found`, `turn_not_found`, `citation_not_found`); `listSessions` filters by `list_reachable_project_ids` at query time. A citation also needs its document's logical index to be mapped to the turn's project at that moment (section 3.5). A session whose project the caller can't reach now is hidden: it is absent from `listSessions`, its reads answer `404 session_not_found`, its feedback `404 turn_not_found`, and `createChatTurn` refuses it through the project check. Nothing is changed or deleted, so it reappears when access returns.
5. **`403` versus `404`.** `403` is used only for `role_not_assigned` (any route) and `admin_required` (any `/v1/admin` route). Every missing or unreachable resource answers `404` with its resource's code, and that body is byte-identical, apart from `correlationId`, to the body for a random identifier, so existence never leaks.
6. **Administrator routes.** `require_admin` runs at step 4 through the route class, before validation and every lookup, so a `User` calling an administrator route with any body and any IDs gets `403 admin_required`, never `404`, `412`, `422` or `428`. Administrators act on every project, index, document, upload and run; administrator routes have no per-project restriction. An administrator gets no wider access to another user's sessions, turns, feedback or memory than any user has: none.

#### 1.4.3 The access functions

| Function | Reads | Result | Refusal |
|---|---|---|---|
| `require_admin(principal)` | nothing | `None` | `AdminRequired` → `403 admin_required` |
| `await authorize_project_for_chat(principal, project_id)` | query Q1 | `AccessibleProject` | `ProjectNotFound` → `404 project_not_found`; the session, turn and citation paths translate it to their own `404` code |
| `await resolve_chat_index(principal, project)` | query Q2, then the index-record cache (query Q3 on a miss) | `ResolvedIndex` | `ProjectIndexUnavailable` → `409 project_index_unavailable` |
| `await list_reachable_project_ids(principal)` | query Q4 | `frozenset[UUID]`, possibly empty | none |
| `await list_my_projects(principal, after, limit)` | query Q5 | rows for `listMyProjects` | none |

- `AccessibleProject`: `id`, `name`, `description`, `instructions`, `example_questions` (tuple of strings), `access` (`admin` or `member`; `admin` when both apply).
- `ResolvedIndex`: `search_index_id`, `source` (`user_override` or `project_default`), `override_expires_at` (datetime or `None`), `physical_index` (`id`, `name`, `schema_version`, `embedding_model`, `embedding_dimensions`, `query_embedding_deployment`).
- **Index resolution precedence:** (1) the caller's `app.user_index_overrides` row for the project whose `expires_at` is later than `SYSUTCDATETIME()`; (2) the project's `app.project_indexes` row with `is_default = 1`. The logical index then resolves to its active physical index: the `app.search_indexes` row joined with the `app.physical_indexes` row its `active_physical_index_id` names, in state `active`. No override and no default mapping, or no active physical index, is `409 project_index_unavailable`; a logical index in `reingest_required`, `reingesting` or `rebuilding` still serves from its active physical index. When an override is used, the span attribute `docchat.access.index_source` is `user_override` and the custom event `index_override_used` fires. Expired overrides are ignored and never deleted automatically; operators set and clear overrides (`86-operations.md` section 3), and no API operation changes them.

#### 1.4.4 The access queries

Each check is one parameterised, index-backed statement run on every request; every value is a bound parameter (SQLAlchemy 2.1 Core), and the literal `1` in `is_default = 1` stays in the statement text so SQL Server can match the filtered index. Each statement shape is one constant text per case (with or without a cursor), never an `:x IS NULL OR …` predicate.

**Q1, `authorize_project_for_chat`** (seeks on `PK_projects` and `PK_project_members`):

```sql
SELECT p.id, p.name, p.description, p.instructions, p.example_questions_json, p.status,
       CASE WHEN m.user_object_id IS NULL THEN 0 ELSE 1 END AS is_member
FROM app.projects AS p
LEFT JOIN app.project_members AS m
       ON m.user_object_id = :oid AND m.project_id = p.id
WHERE p.id = :project_id;
```

No row, `status <> 'active'`, or neither `is_admin` nor `is_member = 1` → `ProjectNotFound`; otherwise `AccessibleProject` with `access = 'admin'` when `is_admin`, else `'member'`.

**Q2, `resolve_chat_index`, override or default** (seeks on `PK_user_index_overrides` and `UX_project_indexes_default`):

```sql
SELECT o.search_index_id AS override_index_id, o.expires_at AS override_expires_at,
       d.search_index_id AS default_index_id
FROM (SELECT 1 AS anchor) AS a
LEFT JOIN app.user_index_overrides AS o
       ON o.user_object_id = :oid AND o.project_id = :project_id AND o.expires_at > SYSUTCDATETIME()
LEFT JOIN app.project_indexes AS d
       ON d.project_id = :project_id AND d.is_default = 1;
```

The override wins when present; neither present → `ProjectIndexUnavailable`.

**Q3, the index record** (on a cache miss only; primary-key seeks):

```sql
SELECT s.id, s.status, p.id AS physical_index_id, p.name, p.schema_version,
       p.embedding_model, p.embedding_dimensions, p.query_embedding_deployment
FROM app.search_indexes AS s
LEFT JOIN app.physical_indexes AS p
       ON p.id = s.active_physical_index_id AND p.state = N'active'
WHERE s.id = :search_index_id;
```

No row, or no active physical index → `ProjectIndexUnavailable`.

**Q4, `list_reachable_project_ids`.** For an administrator, `SELECT p.id FROM app.projects AS p WHERE p.status = N'active';` (seek on `IX_projects_status_name`). For anyone else:

```sql
SELECT m.project_id
FROM app.project_members AS m
JOIN app.projects AS p ON p.id = m.project_id
WHERE m.user_object_id = :oid AND p.status = N'active';
```

**Q5, `list_my_projects`**, administrator form:

```sql
SELECT TOP (:limit_plus_one) p.id, p.name, p.description, p.example_questions_json,
       CASE WHEN ph.id IS NOT NULL THEN 1 ELSE 0 END AS has_default_index
FROM app.projects AS p
LEFT JOIN app.project_indexes AS d ON d.project_id = p.id AND d.is_default = 1
LEFT JOIN app.search_indexes AS s ON s.id = d.search_index_id
LEFT JOIN app.physical_indexes AS ph ON ph.id = s.active_physical_index_id AND ph.state = N'active'
WHERE p.status = N'active'
ORDER BY p.name;
```

The form for anyone else has the same columns and the same three `LEFT JOIN`s, with `FROM app.project_members AS m JOIN app.projects AS p ON p.id = m.project_id` and `WHERE m.user_object_id = :oid AND p.status = N'active'`, `ORDER BY p.name`. With a cursor both add `AND p.name > :after_name`. `has_default_index` is 1 exactly when Q2's default branch and Q3 would resolve the project's default to an active physical index, so the project picker and `createChatTurn` agree.

**Q6, user search** (`adminSearchUsers`), prefix form:

```sql
SELECT TOP (:limit_plus_one) u.object_id, u.display_name, u.email, u.last_known_role,
       u.first_seen_at, u.last_seen_at,
       CASE WHEN m.user_object_id IS NULL THEN 0 ELSE 1 END AS is_member
FROM (SELECT object_id FROM app.users WHERE display_name LIKE :prefix ESCAPE N'\'
      UNION
      SELECT object_id FROM app.users WHERE email LIKE :prefix ESCAPE N'\') AS hit
JOIN app.users AS u ON u.object_id = hit.object_id
LEFT JOIN app.project_members AS m
       ON m.user_object_id = u.object_id AND m.project_id = :project_id
ORDER BY u.display_name, u.object_id;
```

`:prefix` is `q` with `\`, `%`, `_` and `[` each escaped by a preceding `\`, followed by `%`. With a cursor the statement adds `WHERE (u.display_name > :d OR (u.display_name = :d AND u.object_id > :u))`. The UUID form replaces the `hit` subquery with `WHERE u.object_id = :oid`; the form without `q` reads `app.users` in `IX_users_display_name` order. The `LEFT JOIN` and `is_member` appear only when `projectId` is given.

#### 1.4.5 What is cached

Memberships, overrides, mappings and project status are never cached, so a removed membership, an archive or an unmapping takes effect on the next request; projects aren't cached either, because Q1 returns the project in the same round trip. Cached in process for at most 60 seconds: the ops-config snapshot (ADR 0028) and the index-record cache of `resolve_chat_index` (a logical index's status and its active physical index, keyed by `search_index_id`, at most 256 entries, 60-second lifetime). Only a record that has an active physical index is cached, so chat works from the first request after an index's first physical index becomes active, the moment `listMyProjects` starts reporting `hasDefaultIndex: true`; ingestion deletes a retired physical index no sooner than 10 minutes after a repoint (ADR 0045), so a cached record never names a deleted index. Three in-process structures hold no access data and decide no access: the signing-key set (section 1.2), the last-seen throttle map (section 3.2.1) and the burst limiter's buckets (section 1.11).

#### 1.4.6 Revocation latency

Removing a membership, archiving a project or removing a mapping takes effect on the caller's next request. Removing a user's app-role assignment, or their assignment to the application, takes effect when their current access token expires, 60 to 90 minutes after issue (Entra ID's default lifetime), because roles come only from the token. Disabling the account and revoking its sign-in sessions stops new tokens (`86-operations.md` section 3 gives the order of an urgent removal).

### 1.5 Problem details

Every non-`2xx` response the app produces is an RFC 9457 problem with `Content-Type: application/problem+json`, built from the model `docchat_api.http.problems.Problem` (ADR 0012):

| Member | Type | Present | Value |
|---|---|---|---|
| `type` | string | always | `urn:docchat:problem:<code>` |
| `title` | string, 1 to 40 characters | always | the code's one title (section 2) |
| `status` | integer, 400 to 599 | always | the HTTP status |
| `detail` | string, 1 to 300 characters | always | the code's one safe text (section 2); the only text with a variable is `batch_too_large`'s, whose `{maxFilesPerBatch}` is filled with the setting's value |
| `code` | string, 1 to 64 characters, `^[a-z][a-z0-9_]*$` | always | the stable code |
| `correlationId` | string, `^[0-9a-f]{32}$` | always | the request's trace ID (section 1.6) |
| `retryAfterSeconds` | integer, 1 to 2,678,400 | on every `429`; on `503 authentication_unavailable` (30), `503 dependency_unavailable` (5) and `503 malware_scanning_not_ready` (3,600); on `409 turn_in_progress` | the seconds to wait; the response also carries `Retry-After` with the same integer. 2,678,400 is a 31-day month, the longest wait (the monthly token quota) |
| `errors` | array of 1 to 50 `FieldError` | on every `422`, generic or operation-specific | one entry per failed field, at most the first 50 in the order the validator reports them |
| `sessionId` | UUID string | on the four `409` codes of turn creation: `turn_in_progress`, `turn_already_ended`, `client_turn_id_conflict`, `session_turn_limit` | the session the conflict concerns |
| `turnId` | UUID string | on `turn_in_progress`, `turn_already_ended` and `client_turn_id_conflict` (not on `session_turn_limit`, which refuses before any turn exists) | the turn the conflict concerns |

`FieldError` (`docchat_api.http.problems.FieldError`): `path` (string, 1 to 256 characters, section 1.7) and `reason` (string, one of the reasons of section 2.5). It never carries the input value.

Rules:

- The optional members `retryAfterSeconds`, `errors`, `sessionId` and `turnId` are omitted when they don't apply, never sent as `null`; `instance` is never used, and no other member ever appears. The OpenAPI `Problem` schema declares `retryAfterSeconds` with `minimum: 1, maximum: 2678400` and `sessionId` and `turnId` as optional UUID strings, so the web client's generated schema accepts every code's body.
- A problem body never holds exception text, stack traces, SQL, upstream messages, input values, names, titles, file names or e-mail addresses. Each code has one title and one `detail`, the same on every occurrence; the client branches on `code` and on the typed members, never on text.
- Answers from platform authentication (`401`, `403`) and from platform CORS aren't the app's and carry no problem body. The web client treats a `401` without a problem body like `invalid_token` and a `403` without one as an HTTP failure, never as "not assigned" (`10-web-client.md` section 4).
- A code that exists only as a stream `error` event or as a stored turn code is never sent as a problem (section 2.1).

### 1.6 Correlation IDs

`correlationId` is the 32-character lower-case hexadecimal W3C trace ID of the request's server span. The web client sends a fresh `traceparent` on every call, and the Azure Monitor OpenTelemetry distro's FastAPI instrumentation continues that trace, so the ID a user reports is the trace the client started; a request without a valid `traceparent` gets a new trace ID. The ID appears in every problem body, in the `meta` and `error` events of the chat stream, and in `app.audit_events.correlation_id` of every audit row the request writes. No response header carries it, because cross-origin scripts can't read response headers that platform CORS doesn't expose. The request span carries `enduser.id` = the caller's `oid`. Operator commands create one random trace ID per run and print it (`86-operations.md` section 2).

### 1.7 Input validation and limits

**Models.** Every request body is a Pydantic model with `extra="forbid"`, and each operation's query parameters are one Pydantic query model with `extra="forbid"`, so an unknown body field answers `422` with reason `unknown_field` at `body.<field>` and an unknown query parameter the same at `query.<name>`. The headers an operation reads are `If-Match` (section 1.9) and, on `adminUploadContent` alone, `Content-Length` and `Content-Type` (section 3.11.8). Operations without a request body declare no body model: `adminAddProjectMember` answers a non-empty body with `422` at `{path: "body", reason: "unknown_field"}`, and the `GET` and `DELETE` operations never read a body. The bodies of `adminStartReingest` and `adminReingestDocument` are required and are exactly the empty object `{}`: a missing body answers `422` at `{path: "body", reason: "required"}` and any member answers `unknown_field` at `body.<member>`.

**Route options.** Every route declares its body rules as route metadata, `docchat_api.http.route_options(body_limit, content_types, burst_limited, write_limited)`, which the route class and the request-size and content-type guard read:

| Routes | `body_limit` | `content_types` | `burst_limited` | `write_limited` |
|---|---|---|---|---|
| every route not listed below (the defaults) | 65,536 bytes | `('application/json',)`, with the charset parameter absent or `utf-8` | `True` | `True` on `POST`, `PUT`, `PATCH` and `DELETE`; `False` on `GET` |
| `createChatTurn` | 32,768 bytes | `('application/json',)` | `False` | `False` |
| `adminPreflightUploads`, `adminReserveUpload`, `adminSubmitUploads` | 65,536 bytes | `('application/json',)` | `True` | `False` |
| `adminUploadContent` | `None` (no JSON limit; the route enforces `DOCCHAT_UPLOAD_MAX_BYTES` while it streams) | `('application/octet-stream',)` | `True` | `False` |

A body sent with a media type the route doesn't declare answers `415 unsupported_media_type`, with one text for every route. A body whose `Content-Length` exceeds the route's limit, or whose streamed bytes exceed it, answers `413 request_too_large`. Invalid JSON or invalid UTF-8 answers `422` with `{path: "body", reason: "invalid_json"}`. A unit test pins that exactly these four routes deviate from the defaults.

**Field paths.** `errors[].path` is the location (`body`, `query`, `path` or `header`) followed by the dotted path in the API's camelCase names with zero-based list indices: `body.exampleQuestions.2`, `body.methodParameters.targetTokens`, `query.limit`, `path.projectId`, `header.If-Match`, `header.Content-Length`.

**Reasons.** `errors[].reason` comes from the open catalogue `docchat_core.errors.REASONS` (section 2.5). Pydantic error types map to reasons as follows: `missing` → `required`; `extra_forbidden` → `unknown_field`; `json_invalid` and `json_type` → `invalid_json`; `uuid_parsing`, `uuid_type` and pattern failures on identifier fields → `invalid_uuid`; `string_too_short` → `too_short`; `string_too_long` → `too_long`; `too_short` on a list → `too_short`; `too_long` on a list → `too_many_items`; `greater_than`, `greater_than_equal`, `less_than` and `less_than_equal` → `out_of_range`; `literal_error` and `enum` → `not_allowed`; every other `*_type` error → `invalid_type`; errors raised by the text types and validators carry their own reason (`invalid_characters`, `duplicate`, `invalid_cursor`, `empty`); anything else → `invalid_format`. Every `422`, generic or operation-specific, carries at least one `errors[]` entry.

**Identifiers.** Every identifier in a path, query or body must match `^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$` and is lower-cased after validation; anything else (braces, a URN, 32 hexadecimal digits without hyphens) answers `422` with reason `invalid_uuid`. A malformed identifier therefore never reaches a lookup, and a `404` always means "well formed, and absent or unreachable".

**Text types** (`docchat_api.http.text`), used by the administrator inputs:

- `SingleLineText(min, max)`: Unicode NFC; leading and trailing whitespace removed; every run of whitespace (tabs and line breaks included) collapsed to one space; any code point of category Cc, Cf, Cs, Co or Cn refused with reason `invalid_characters`; length measured in code points after normalisation (`too_short`, `too_long`).
- `MultiLineText(min, max)`: Unicode NFC; CR LF and lone CR turned into LF; leading and trailing whitespace of the whole text removed; LF and TAB allowed; every other Cc code point and every code point of category Cf, Cs, Co or Cn refused with `invalid_characters`; length in code points after normalisation.

Refusing category Cf (the bidirectional controls U+202A to U+202E and U+2066 to U+2069, the zero-width characters U+200B to U+200F and U+FEFF, and the rest of the category) keeps a text one administrator enters from reordering or hiding characters on another person's screen; the accepted cost is that a text can't hold the joiners U+200C and U+200D. The chat question, the feedback comment, memory fact text and upload file names have their own normalisation rules, stated with their operations.

**Bounds and shapes.** Every string, list and integer of every model has explicit bounds (`minLength` and `maxLength`, `minItems` and `maxItems`, `minimum` and `maximum`), and every closed set is an enumeration; all of them appear in the OpenAPI document. Lengths are counted in Unicode code points after the field's normalisation. Request fields are non-nullable unless the operation says otherwise; in a `PATCH`, an absent field means "unchanged". Responses always include every field of their model, `null` where empty (the optional members of `Problem` excepted). Times are ISO 8601 UTC with milliseconds and `Z` (`2026-10-21T09:15:02.123Z`); identifiers are lower-case hyphenated UUIDs; SHA-256 values are 64 lower-case hexadecimal characters.

### 1.8 Pagination

Every list operation takes `limit` (an integer whose range and default the operation states; the maximum is never above 100) and `cursor` (optional, 1 to 512 characters, `^[A-Za-z0-9_-]+$`), and answers `{"items": [...], "nextCursor": string | null}`. Two operations name their members differently: `getSession` returns its newest turns as `turns` with `earlierTurnsCursor`, and `adminGetIngestionRun` returns its items as `items` with `nextItemsCursor` (its parameters are `itemsLimit` and `itemsCursor`).

- Every list has a total order ending in a unique column; paging is keyset, never offset. The server reads `limit + 1` rows and returns a cursor only when the extra row exists; the last page has `nextCursor: null`.
- A cursor is the base64url encoding, without `=` padding, of compact UTF-8 JSON `{"v": 1, …}` holding the sort values of the last item returned and the filter values the cursor was issued for. Encoding: `json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")`, then URL-safe base64 with the padding removed. Decoding: length 1 to 512 and the pattern above; restore the padding; base64url-decode; parse the JSON; validate with the operation's cursor model (`extra="forbid"`, `v` equal to 1); compare its filter members with the request's (after the operation's normalisation of each filter). Any failure answers `422` with `{path: "query.cursor", reason: "invalid_cursor"}` (`query.itemsCursor` on `adminGetIngestionRun`).
- Cursors aren't signed: they hold only positions inside the caller's own authorised view, so tampering only skips or repeats items the caller is allowed to see anyway.

| Operation | Limit parameter, range (default) | Order | Cursor JSON | Filter members |
|---|---|---|---|---|
| `listMyProjects` | `limit` 1 to 100 (50) | `name` | `{"v":1,"n":"<name>"}` | none |
| `adminListProjects` | `limit` 1 to 100 (50) | `name` | `{"v":1,"n":"<name>","s":"<status filter>","q":"<q>" or null}` | `s`, `q` |
| `adminListProjectMembers` | `limit` 1 to 100 (50) | `displayName`, then `userId` | `{"v":1,"d":"<displayName>","u":"<userId>"}` | none |
| `adminSearchUsers` | `limit` 1 to 100 (50) | `displayName`, then `userId` | `{"v":1,"d":"<displayName>","u":"<userId>","q":"<q>" or null,"p":"<projectId>" or null}` | `q`, `p` |
| `adminListProjectIndexes` | `limit` 1 to 100 (50) | the default mapping first, then `displayName`, then `indexId` | `{"v":1,"f":0 or 1,"d":"<displayName>","i":"<indexId>"}` (`f` is 1 for the default mapping) | none |
| `listSessions` | `limit` 1 to 50 (20) | `lastTurnAt` descending, then `sessionId` descending | `{"v":1,"t":"<lastTurnAt>","s":"<sessionId>","p":"<projectId>" or null}` | `p` |
| `getSession` (`earlierTurnsCursor`) and `listSessionTurns` | `turnsLimit` on `getSession`, `limit` on `listSessionTurns`, 1 to 50 (20) | pages from the newest turn to the oldest; turns ascending by `seq` within a page | `{"v":1,"b":<seq>}`: the page holds turns with `seq < b`; `b` is an integer from 2 to `turnCount + 1` | none |
| `adminListIndexes` | `limit` 1 to 100 (50) | `displayName`, then `indexId` | `{"v":1,"n":"<displayName>","i":"<indexId>","s":"<status>" or null,"q":"<q>" or null}` | `s`, `q` |
| `adminListDocuments` | `limit` 1 to 100 (50) | the normalised name, then `documentId` | `{"v":1,"n":"<normalised name>","i":"<documentId>","s":"<state>" or null,"q":"<normalised q>" or null}` | `s`, `q` |
| `adminListIngestionRuns` | `limit` 1 to 100 (50) | `createdAt` descending, then `runId` descending | `{"v":1,"c":"<createdAt>","r":"<runId>","x":"<indexId>" or null,"s":"<state>" or null,"k":"<kind>" or null}` | `x`, `s`, `k` |
| `adminGetIngestionRun` (items) | `itemsLimit` 1 to 100 (100) | the item's queue sequence | `{"v":1,"r":"<runId>","a":<queue sequence of the last item>,"s":"<itemState>" or null}` | `r`, `s` |

A session that receives a new turn while its owner pages through `listSessions` moves to the top and can appear on two pages; the client de-duplicates by `sessionId`.

### 1.9 Optimistic concurrency

**Version tokens.** SQL resources carry `rowVersion`: the 8-byte `ROWVERSION` as standard base64 with padding, 12 characters matching `^[A-Za-z0-9+/]{11}=$`, present in every response body that carries the resource (`AdminProject`, `AdminProjectSummary`, `IndexSummary`, `IndexDetail`, `DocumentSummary`, `DocumentDetail`) and, as `projectRowVersion`, in `ProjectMemberChange`, `ProjectIndexMappingChange` and `ProjectPartRemoval`. The memory document carries `etag`: its Cosmos DB `_etag`, verbatim, double quotes included, in `MemoryView`. The tokens travel in bodies because cross-origin scripts can't read an `ETag` header; the API sends no `ETag` header.

**The 14 operations that require `If-Match`:**

| Operation | Version it must match |
|---|---|
| `adminUpdateProject`, `adminAddProjectMember`, `adminRemoveProjectMember`, `adminMapProjectIndex`, `adminUnmapProjectIndex` | the project's `rowVersion`; members and mappings are parts of the project, so a change to either moves the project's version |
| `adminUpdateIndex`, `adminStartReingest`, `adminDeleteIndex` | the logical index's `rowVersion` |
| `adminDeleteDocument`, `adminReingestDocument` | the document's `rowVersion` |
| `updateMemorySettings`, `updateMemoryFact`, `deleteMemoryFact`, `clearMemory` | the memory document's `etag` |

Creates (`adminCreateProject`, `adminCreateIndex`), the upload operations (guarded by the SHA-256 constraints and the version state, `14-ingestion.md` section 2) and `putTurnFeedback` (last write wins, section 3.4.4) take no `If-Match`.

**Parsing** (one parser in the package `docchat_api.http`, used by all 14 operations):

1. No `If-Match` header → `428 precondition_required`.
2. Repeated headers are joined with `,` and the value is trimmed. The value `*`, a value starting with `W/`, or any value containing `,` → `428`.
3. The value must start and end with `"` and hold at least one character between them, else `428`.
4. For a `rowVersion`: the text between the quotes must match `^[A-Za-z0-9+/]{11}=$`, else `412 precondition_failed` (it can never be current); its 8 decoded bytes are compared with the row's `row_version`, and a mismatch is `412`. For a memory `etag`: the whole value, quotes included, is compared with the document's `_etag`, and a mismatch is `412`.

**Placement and atomicity.** The header is parsed before the operation's lookups, and its `428` and `412` are answered at step 9 of section 1.3: after every lookup (`404`) and before every state rule (`409`). For a SQL resource the operation reads the row with `UPDLOCK, ROWLOCK` inside its transaction, compares the version (`412`), applies its state rules, and writes with a guarded `UPDATE … WHERE id = @id AND row_version = @rowVersion` (or, for members and mappings, the project-version move of section 3.7.1); a guarded update that changes zero rows triggers one re-read, which answers `404` when the row is gone and `412` when its version moved. For the memory document the operation reads it, looks up the fact (`404`), compares the `etag` (`412`), applies its rules and writes with `replace_item` conditioned on the same ETag; a `412` from Cosmos DB at that write answers `412` to the client. A call that changes nothing (an existing membership, an unchanged mapping, an absent row to remove, a body equal to the stored values, an unchanged pause flag) moves no version and returns the current one.

**Client contract.** The client sends the body value verbatim when it starts and ends with `"` (memory ETags), otherwise wrapped in double quotes (the `rowVersion` `AAAAAAAAB9E=` is sent as `If-Match: "AAAAAAAAB9E="`). After each successful write it keeps the new `rowVersion`, `projectRowVersion` or `etag` from the response, so one administrator's consecutive changes chain; on `412` it reloads (ADR 0094). The administrator screens read `adminGetProject` before the member and mapping lists they show, so the project version they send is never newer than what they display.

### 1.10 Response headers

| Header | Value | On |
|---|---|---|
| `Cache-Control` | `no-store` | every response the app produces, problems and `GET /healthz` included |
| `X-Content-Type-Options` | `nosniff` | every response |
| `Referrer-Policy` | `no-referrer` | every response |
| `Strict-Transport-Security` | `max-age=31536000` | every response |
| `Content-Security-Policy` | `default-src 'none'; frame-ancestors 'none'` | every JSON, problem and NDJSON response |
| `Content-Security-Policy` | `sandbox` | file responses (`getCitationContent`, `getCitationDocument`, `adminGetDocumentPdf`) |
| `Content-Disposition` | built only by `docchat_api.http.content_disposition(name)` (below) | file responses |
| `Retry-After` | the integer of `retryAfterSeconds` | every problem that carries `retryAfterSeconds` |
| `WWW-Authenticate` | section 1.2 | every in-app `401` |
| `Allow` | the path's methods | every `405` |

- No `Server` header and no access log: the worker class sets `server_header=False` and `access_log=False` (section 1.15), because request lines would carry search text and identifiers into logs; request telemetry comes from spans.
- No `ETag` header (version tokens travel in bodies, section 1.9), and no `Location` header on a `201` or `202` (the body carries the new resource's identifiers).
- No response compression on any route: the app registers no compression middleware and never sends `Content-Encoding`.
- No CORS header from the app, in any environment: the app runs no CORS code. In deployed environments App Service platform CORS answers preflights for the web client's origins only (the Static Web Apps origin and each custom domain, exact origins, credentials not supported), as `83-infrastructure.md` section 2 configures it; the web client sends exactly the request headers `Authorization`, `Content-Type`, `Accept`, `If-Match` and `traceparent`, and no response header is exposed, so every value the client needs travels in the body. Locally the web client's development server proxies `/v1/` and `/healthz` to the API, so local calls are same-origin (`10-web-client.md` section 1).

**`content_disposition(name)`** returns `attachment; filename="<fallback>"; filename*=UTF-8''<encoded>` (RFC 6266 with RFC 8187):

1. The name in Unicode NFC is cut to 200 code points, keeping its extension: when it is longer, the part before the last `.` is shortened until that part, the last `.` and what follows total 200 code points; a name without `.` is cut to 200.
2. `<encoded>`: the cut name's UTF-8 bytes, with every byte outside RFC 8187's `attr-char` set (ASCII letters, digits, `!`, `#`, `$`, `&`, `+`, `-`, `.`, `^`, `_`, the grave accent U+0060, `|` and `~`) percent-encoded as `%XX` with upper-case hexadecimal digits.
3. `<fallback>`: the cut name with every code point outside printable ASCII (0x20 to 0x7E) and each of `"`, `\`, `/`, `;` and `%` replaced, one for one, by `_`, so it is at most 200 characters and keeps the extension.
4. The disposition is always `attachment`: the web client fetches the bytes and shows them from a blob URL (`10-web-client.md` section 8), so `inline` adds nothing, and `attachment` keeps a response opened by direct navigation from rendering in the API's origin.

### 1.11 Request limits on every route

Every route has a stated limit or the explicit statement that it has none:

| Operations | Limit | Answer when exceeded | Specified in |
|---|---|---|---|
| `getHealth` | none | – | – |
| every `/v1` operation except `createChatTurn` (43 operations) | burst limit: an in-process token bucket per user and worker process, 40 tokens refilled at 10 per second | `429 too_many_requests`, `retryAfterSeconds` 1 | below |
| the 17 write-limited operations | write window: `api.write_requests_per_minute` (default 120, range 10 to 1,200) per user over a sliding minute, shared by every instance | `429 write_rate_limited`, `retryAfterSeconds` 1 to 120; `503 dependency_unavailable` (`retryAfterSeconds` 5) when the window can't be read or written | below |
| `createChatTurn` | `ratelimit.chat_requests_per_minute` (default 3, range 1 to 1,200) per user over a sliding minute; `ratelimit.max_concurrent_streams` (default 2, range 1 to 200) streams per user; `quota.tokens_per_day` (default 2,000,000) and `quota.tokens_per_month` (default 32,000,000) tokens per user; `chat.max_turn_seconds` (default 180, range 60 to 220) per turn | `429 rate_limited` (`retryAfterSeconds` 1 to 120), `429 too_many_streams` (10), `429 token_quota_exceeded` (the seconds to the next UTC midnight or month start); `503 dependency_unavailable`; a turn past its deadline ends with the stream code `turn_timeout` | `12-chat-pipeline.md` section 3 |
| `getCitationContent`, `getCitationDocument` and `adminGetDocumentPdf`, counted together | `ratelimit.citation_downloads_per_minute` (default 30, range 1 to 500) per user over a sliding minute; `ratelimit.max_concurrent_downloads` (default 2, range 1 to 10) per user; every 64 KiB piece accepted by the client within 30 s; 300 s per download; at most 20 MiB for a page rendition and 104,857,600 bytes for a whole-document rendition, the sizes ingestion lets through (`14-ingestion.md` sections 14.4 and 15.6) | `429 rate_limited` (`retryAfterSeconds` 1 to 120), `429 too_many_downloads` (10); `503 dependency_unavailable`; a slow or overlong download is aborted (sections 3.5.2 and 3.5.3) | `12-chat-pipeline.md` section 16 |
| `adminUploadContent` | `DOCCHAT_UPLOAD_CONCURRENT_STREAMS_PER_WORKER` (default 4, range 1 to 16) concurrent uploads per worker process; `DOCCHAT_UPLOAD_MAX_BYTES` (default 52,428,800 bytes, range 1,048,576 to 209,715,200) per file | `429 upload_busy` (`retryAfterSeconds` 5); `413 file_too_large` | section 3.11.8 |
| `adminPreflightUploads`, `adminSubmitUploads` | `DOCCHAT_UPLOAD_MAX_FILES_PER_BATCH` (default 100, range 1 to 100) files or versions per call | `422 batch_too_large` | sections 3.11.6, 3.11.9 |
| every route with a JSON body | 65,536 bytes per body (32,768 on `createChatTurn`) | `413 request_too_large` | section 1.7 |

**Burst limit** (`docchat_api.http.ratelimit.BurstLimiter`, step 3a; one limiter per Gunicorn worker process; ADR 0143):

```text
now = monotonic clock
bucket = buckets.get(oid) or new bucket {tokens: 40, at: now}
move oid to the most recently used end; evict the least recently used while the map holds more than 50,000 users
bucket.tokens = min(40, bucket.tokens + 10 × (now − bucket.at)); bucket.at = now
if bucket.tokens < 1: count docchat.ratelimit.rejections (reason api); answer 429 too_many_requests, retryAfterSeconds 1, Retry-After: 1
bucket.tokens = bucket.tokens − 1; allow
```

The check runs in memory on the event loop in well under 1 ms and takes no lock, because each worker process serves its event loop alone. It runs before provisioning, the ops-config snapshot or any store. The buckets hold no access decision, so losing them (a restart, an eviction, a request served by another worker or instance) only loosens the limit; a user's ceiling therefore grows with workers and instances, which suits a flood guard. The three values are code constants, not settings: a page load issues about 10 requests and the web client polls a screen at most every 5,000 ms, so normal use stays far below them.

**Write limit** (`docchat_api.http.ratelimit.WriteRateLimiter`, step 4a; ADR 0143). The window documents are `wrate:{epochMinute}` in the caller's partition of the Cosmos DB container `usage` (`80-data-model.md` section 3): `id` `wrate:{epochMinute}` (whole minutes since 1970-01-01T00:00:00Z), `userId` (the caller's `oid`), `type` `write_rate_window`, `count` (integer ≥ 1), `ttl` 180.

```text
L = the request's ops-config snapshot: api.write_requests_per_minute
for attempt in 1..5:
    t = now in UTC epoch seconds; m = floor(t / 60); f = (t − 60m) / 60
    prev, cur = concurrently read_item(wrate:{m−1}), read_item(wrate:{m})     # timeout 2 s each; 404 → count 0
    if (cur.count + 1) + prev.count × (1 − f) > L:
        count docchat.ratelimit.rejections (reason write)
        answer 429 write_rate_limited with retryAfterSeconds = the smallest whole s from 1 to 120 for which
            the estimate at t + s, with no further requests, allows one more request (stepping 1 s at a time)
    if cur exists: patch_item(wrate:{m}, [incr /count 1]) conditioned on cur's ETag     # timeout 3 s; 412 → next attempt
    else: create_item({id: wrate:{m}, userId, type: write_rate_window, count: 1, ttl: 180})   # timeout 3 s; 409 → next attempt
    allow
answer 429 write_rate_limited, retryAfterSeconds 1
any Cosmos DB failure after the SDK's retries (connection error, timeout, 429, 5xx): answer 503 dependency_unavailable, retryAfterSeconds 5
```

A refused request isn't counted; an allowed one counts even when a later step refuses it (`404`, `409`, `412`, `422`). The check fails closed, because an unenforced limit is the flood it exists to stop. The write-limited set is the route-option default: every `POST`, `PUT`, `PATCH` and `DELETE` route is write-limited unless it declares `write_limited=False`, and exactly five routes do (`createChatTurn` and the four upload operations); a unit test pins that set. An allowed write costs two point reads and one patch or create in the caller's partition.

**Test seam.** `create_app` receives the `BurstLimiter` and the `WriteRateLimiter` as ports; tests pass permissive implementations except tests whose subject is a limiter's answer on a route. No setting disables a limiter in a deployed app.

### 1.12 Idempotency

| Operation | A repeated request |
|---|---|
| every `GET` | changes nothing; `getCurrentUser` and every other first request of a user provision the user once (section 3.2.1); `getMemory` creates the empty memory document once |
| `createChatTurn` | `clientTurnId` makes the turn idempotent: the session and turn IDs derive from it (UUID version 5, section 3.3.1). A duplicate of a `completed` turn replays the stored answer (`200`, the same stream shape, no model call and no write); of a `streaming` turn before its deadline, `409 turn_in_progress`; of a `failed`, `cancelled` or abandoned turn, `409 turn_already_ended`; with another question or project, `409 client_turn_id_conflict`. A retry sends a new `clientTurnId` |
| `putTurnFeedback` | the same body stores the same value; `updatedAt` moves and `createdAt` stays |
| `updateMemorySettings`, `updateMemoryFact`, `deleteMemoryFact`, `clearMemory` | a repeat with the `etag` sent before a successful change gets `412`; an unchanged value (the same `paused`, the same text and category, an empty memory to clear) answers `200` with no write |
| `adminCreateProject`, `adminCreateIndex` | a repeat with the same name gets `409 project_name_taken` or `409 display_name_taken`, so no duplicate is created |
| `adminUpdateProject`, `adminUpdateIndex` | a body equal to the stored values answers `200` with nothing changed; a repeat with the version sent before a change gets `412` |
| `adminAddProjectMember`, `adminMapProjectIndex` | an existing membership or an unchanged mapping answers `200` with the unchanged `projectRowVersion`; a repeat with the version sent before a successful change gets `412` |
| `adminRemoveProjectMember`, `adminUnmapProjectIndex` | an absent row answers `200` with `removed: false` and the unchanged version |
| `adminDeleteIndex`, `adminStartReingest`, `adminDeleteDocument` | a repeat with the version sent before gets `412`; with the new version, `409 index_busy` while the run's status lasts, and `404` once the index or document is deleted |
| `adminReingestDocument` | while an item for the current version and target is `queued` or `running`, the answer is `200` with the newest open run's ID and `itemCount` 0, and no run is created |
| `adminPreflightUploads` | read-only |
| `adminReserveUpload` | an `uploading` version of the document with the same SHA-256 is reused (`reused: true`) |
| `adminUploadContent` | a version already `pending` with the reserved SHA-256 answers `200` with its summary; the blob is committed with `If-None-Match: *`, so a version's path is written once |
| `adminSubmitUploads` | versions already queued report `already_queued`; when nothing is queued the answer is `200` with `runId: null` |

### 1.13 The OpenAPI document and access metadata

- **Export.** `uv run --package docchat-api python -m docchat_api.openapi --output apps/api/openapi.json` writes the OpenAPI 3.1 document from `app.openapi()`, with sorted keys, two-space indentation, `ensure_ascii=False` and a trailing newline, so an unchanged API regenerates identical bytes (ADR 0013). `apps/api/openapi.json` is committed. The CI job `contract` regenerates it and the web client's generated files and fails on any difference, then runs `scripts/ci/check_contract_enums.py` (`01-repository.md` section 7).
- **Per operation:** its operationId (section 1.1); one tag from `health`, `me`, `projects`, `chat`, `sessions`, `memory`, `admin-projects`, `admin-users`, `admin-indexes`, `admin-documents`, `admin-uploads`, `admin-runs`; the security requirement `bearerAuth` (HTTP bearer, bearer format `JWT`), except `getHealth`, which has `security: []`; every error status it can return, each with content `application/problem+json` referencing `#/components/schemas/Problem`; and these extensions:
  - `x-docchat-access`: `public` (only `getHealth`), `user` or `admin` (section 1.4.1).
  - `x-docchat-scoped-params`: every parameter that identifies a resource subject to an existence or reachability check, as a path or query parameter name or as `body:<JSON pointer>` for a body field; an empty list when there is none. Each operation's section lists its entries and the answer each gives for an identifier the caller can't reach; that answer is byte-identical, apart from `correlationId`, for an identifier from another project, user or index and for a random one. For path and query entries the answer is the operation's `404` code for that parameter. Three body entries answer otherwise: `body:/initialMemberIds` on `adminCreateProject` answers `422 validation_failed` with reason `not_found` at `body.initialMemberIds.<i>`; `body:/projectId` on `adminReserveUpload` answers `422 project_not_mapped` with reason `not_allowed` at `body.projectId`; `body:/versionIds` on `adminSubmitUploads` answers `200` with `runId: null` and outcome `not_found` for each such version. At an array pointer the authorisation matrix test substitutes the identifier into every element.
  - `x-docchat-error-codes`: the problem codes the operation can return, sorted: the codes of its common answer sets (section 1.14) and its own codes. The router-level codes `route_not_found` and `method_not_allowed` appear in no operation's list; `getHealth`'s list is empty.
  - `x-docchat-stream: "ndjson-one-event-per-line"` on `createChatTurn`'s `200` response.
- **Schemas.** Schema names are the Pydantic model names of sections 3 and 4; JSON names are the camelCase aliases. `docchat_api.openapi.document_streams()` post-processes the document: every member of the `ChatStreamEvent` union goes into `components.schemas` from `pydantic.TypeAdapter(ChatStreamEvent).json_schema(mode="serialization", ref_template="#/components/schemas/{model}")`; a `ChatStreamEvent` schema with `oneOf` over the eight event models and `discriminator.propertyName` `type`; and `createChatTurn`'s `200` response as `content: {"application/x-ndjson": {"schema": {"$ref": "#/components/schemas/ChatStreamEvent"}}}`.
- **Request examples.** An operation whose valid body the matrix test can't generate from the schema alone carries one OpenAPI request example that the API accepts against the seeded test data (the matrix resolves every identifier in it through its name-to-kind map, `84-testing.md` section 3): `createChatTurn`, `putTurnFeedback`, `updateMemorySettings`, `updateMemoryFact`, `adminCreateProject`, `adminUpdateProject`, `adminMapProjectIndex`, `adminCreateIndex`, `adminUpdateIndex`, `adminPreflightUploads`, `adminReserveUpload` and `adminSubmitUploads`. Each operation's section gives its example.
- **Enumerations evolve client first.** A new value of a response enumeration ships in this order: the API model adds it and the committed document carries it; the web release that understands it deploys; only then does the API return it. `scripts/ci/check_contract_enums.py` fails a pull request that adds a response-enumeration value together with a code path that returns it, unless the pull request carries the label `enum-already-released`.

### 1.14 Common answer sets

Each operation in section 3 lists the common sets that apply to it and then its own codes; together they are its `x-docchat-error-codes`.

| Set | Codes | Applies to |
|---|---|---|
| **Base** | `401 authentication_required`, `401 invalid_token`, `403 role_not_assigned`, `500 internal_error`, `503 authentication_unavailable`, `503 dependency_unavailable` | every operation except `getHealth` |
| **Burst** | `429 too_many_requests` | every operation except `getHealth` and `createChatTurn` |
| **Admin** | `403 admin_required` | every `/v1/admin` operation |
| **Write** | `429 write_rate_limited` (the write check's store failure is Base's `503 dependency_unavailable`) | the 17 write-limited operations |
| **Body** | `413 request_too_large`, `415 unsupported_media_type`, `422 validation_failed` | every operation with a JSON request body |
| **Params** | `422 validation_failed` | every operation with a path or query parameter |
| **Precondition** | `428 precondition_required`, `412 precondition_failed` | the 14 `If-Match` operations |

### 1.15 The server process

- **Gunicorn** runs the app from `apps/api/gunicorn.conf.py`: `bind = "0.0.0.0:8000"`; `workers` = `DOCCHAT_API_WORKERS` (integer 1 to 16; the plan's vCPU count, 1 on B1 and 2 on P1v3, `83-infrastructure.md` section 2); `worker_class = "docchat_api.http.worker.DocChatUvicornWorker"`; `graceful_timeout` = `DOCCHAT_API_GRACEFUL_TIMEOUT_SECONDS` (default 110, range 30 to 110); `timeout` = `DOCCHAT_API_WORKER_TIMEOUT_SECONDS` (default 120, range 30 to 120; the worker heartbeat, which never limits a request under the Uvicorn worker). The image's command is the exec form `CMD ["gunicorn", "--config", "/app/gunicorn.conf.py", "docchat_api.app:create_app()"]`, so Gunicorn is the container's process 1 and receives App Service's stop signal itself (`01-repository.md` section 4).
- **`DocChatUvicornWorker`** subclasses `uvicorn_worker.UvicornWorker`; its `CONFIG_KWARGS` set `server_header=False`, `access_log=False` and `timeout_keep_alive=65`. Its `_serve()` is uvicorn-worker 0.4.0's `_serve()` with `DocChatServer` in place of `uvicorn.Server` (the application, the SIGQUIT handler and the boot-error exit unchanged).
- **`DocChatServer`** (same module) subclasses `uvicorn.Server`. `serve()` stores the running event loop in `self.loop` and then awaits the base `serve()`. `handle_exit(sig, frame)`, the handler Uvicorn installs for SIGINT and SIGTERM, schedules `docchat_api.chat.tasks.begin_drain` on the worker's loop with `self.loop.call_soon_threadsafe(begin_drain)` on the first SIGTERM (a flag makes every later SIGTERM schedule nothing; SIGINT schedules nothing) and then calls the base `handle_exit`, so Uvicorn's own shutdown is unchanged. `begin_drain` is looked up on the module `docchat_api.chat.tasks` when the signal arrives, and it is a plain function, never a coroutine function. `call_soon_threadsafe` is required because a Python signal handler interrupts the loop's own thread between bytecodes, where `call_soon` isn't safe; the drain therefore starts within milliseconds of the signal while the streams are still open. The drain itself (90 s after the signal, turns ended with `service_restarting` and saved before 105 s) is `12-chat-pipeline.md` section 14. Gunicorn's master forwards the container's SIGTERM to every worker as a graceful stop, and App Service waits at most 120 s after it.
- **Lifespan.** Start-up validates the settings, creates every client through `docchat_core.clients` (one `ManagedIdentityCredential` per process, `83-infrastructure.md` section 6), starts the signing-key task and loads the ops-config snapshot; a failure exits the process before it serves. It then warms the SQL connection pool, at most `DOCCHAT_SQL_CONNECT_TIMEOUT_SECONDS`, so the first requests find open connections; a warm-up failure is recorded and never exits the process (`01-repository.md` section 6.2 item 11). Uvicorn serves only after start-up, so `GET /healthz` answers only then. Shutdown cancels the signing-key task and closes every client.

### 1.16 Settings and telemetry of the API layer

**Settings** this file's rules read (each defined in `81-config.md` sections 2 and 3): `DOCCHAT_AUTH_TENANT_ID`, `DOCCHAT_AUTH_API_CLIENT_ID`, `DOCCHAT_AUTH_WEB_CLIENT_ID`, `DOCCHAT_AUTH_TEST_CLIENT_ID`, `DOCCHAT_CORE_ENVIRONMENT`, `DOCCHAT_API_WORKERS`, `DOCCHAT_API_GRACEFUL_TIMEOUT_SECONDS`, `DOCCHAT_API_WORKER_TIMEOUT_SECONDS`, `DOCCHAT_UPLOAD_MAX_BYTES`, `DOCCHAT_UPLOAD_MAX_FILES_PER_BATCH`, `DOCCHAT_UPLOAD_CONCURRENT_STREAMS_PER_WORKER`, `DOCCHAT_STORAGE_MALWARE_SCANNING_ENABLED`, `DOCCHAT_SEARCH_MAX_INDEXES`, `DOCCHAT_OPENAI_EMBEDDING_MODELS`; and the ops-config keys `api.write_requests_per_minute`, `ratelimit.chat_requests_per_minute`, `ratelimit.max_concurrent_streams`, `ratelimit.citation_downloads_per_minute`, `ratelimit.max_concurrent_downloads`, `quota.tokens_per_day`, `quota.tokens_per_month`, `chat.max_turn_seconds`, `conversations.max_turns_per_session`, `memory.enabled`, `memory.max_facts`, `memory.max_tokens` and `feedback.comments_enabled`. Every request takes one immutable ops-config snapshot when it first needs one, and the snapshot refreshes on the first access after it is 60 seconds old (ADR 0028).

**Telemetry** of the API layer (each defined in `85-observability.md` section 4): spans `docchat.auth.validate_token`, `docchat.auth.fetch_signing_keys`, `docchat.users.ensure`, `docchat.access.authorize_project`, `docchat.access.resolve_index`, `docchat.access.reachable_projects`, `docchat.ratelimit.write_check` and `docchat.opsconfig.refresh`; metrics `docchat.auth.validations`, `docchat.auth.signing_key_fetches`, `docchat.auth.signing_key_age`, `docchat.access.denials`, `docchat.users.provisioned`, `docchat.api.problems`, `docchat.config.invalid`, `docchat.opsconfig.refresh_failures`, `docchat.access.index_overrides_used`, `docchat.ratelimit.rejections` and `docchat.audit.write_failures`; custom events `user_provisioned`, `index_override_used`, `admin_change`, `signing_keys_refresh_failed` and `ops_config_invalid`; log records `signing_keys_fetch_failed`, `ops_config_invalid`, `ops_config_unreachable`, `request_failed` and `document_pdf_blob_missing` (section 3.11.10). There is no per-request log record. No token, `Authorization` header, claim other than `oid`, `tid` and `azp`, display name, e-mail address, project name, search text, request body or ops-config value is logged or put on a span (ADR 0014).

## 2. Error-code catalogue

### 2.1 The catalogue in code

`docchat_core.errors.CODES` holds one entry per code of sections 2.2 to 2.4: `code`, `status` (the HTTP status, or `None` for a code that exists only as a stream `error` event or a stored turn code), `title` (sentence case, at most 40 characters), `detail` (the one safe text, at most 300 characters; for a stream code its event `message`), `retryable` (boolean) and `area` (`identity_access`, `ingestion`, `chat` or `conversations`, the area whose code it is). `docchat_core.errors.REASONS` holds every `422` reason of section 2.5. Both are open catalogues: an area that adds a code or a reason adds its entry in the same change. Rules, each enforced by a unit test: no code or reason is registered twice; every code raised anywhere in `apps/api` and `packages/core` is registered with a title and a `detail`; every reason raised is registered; `status` is `None` for exactly the 13 stream-only codes of section 2.3 and `turn_abandoned`, which still have a title and a `detail`; the problem handler never builds a response from an entry whose `status` is `None`, and raising such a code as a `DocChatProblem` fails the test. Texts never contain exception text, upstream messages, input values, names or paths.

"Retryable" below means the same request can succeed later without a change, unless the cell says otherwise; "ra n" means the problem carries `retryAfterSeconds` n and `Retry-After: n`.

### 2.2 Problem codes

| Code | Status | Title | `detail` | Retryable | Extension members | Raised by |
|---|---|---|---|---|---|---|
| `authentication_required` | 401 | Sign-in required | Sign in to continue. | no | – | every operation except `getHealth` |
| `invalid_token` | 401 | Sign-in not accepted | Your sign-in isn't valid any more. Sign in to continue. | no | – | every operation except `getHealth` |
| `role_not_assigned` | 403 | No access | Your account doesn't have access to DocChat. Ask your administrator to give you access. | no | – | every operation except `getHealth` |
| `admin_required` | 403 | Administrator access needed | This needs administrator access. | no | – | every `/v1/admin` operation |
| `route_not_found` | 404 | Not found | There's nothing at this address. | no | – | routing (unknown path) |
| `project_not_found` | 404 | Project not found | This project doesn't exist, or you don't have access to it. | no | – | `createChatTurn`, `listSessions`, `adminGetProject`, `adminUpdateProject`, `adminListProjectMembers`, `adminAddProjectMember`, `adminRemoveProjectMember`, `adminSearchUsers`, `adminListProjectIndexes`, `adminMapProjectIndex`, `adminUnmapProjectIndex` |
| `user_not_found` | 404 | Unknown user | There's no user with this ID. People appear here after they first sign in. | no | – | `adminAddProjectMember` |
| `index_not_found` | 404 | Index not found | The index wasn't found. | no | – | `adminMapProjectIndex`, `adminGetIndex`, `adminUpdateIndex`, `adminDeleteIndex`, `adminStartReingest`, `adminListDocuments`, `adminGetDocument`, `adminGetDocumentPdf`, `adminDeleteDocument`, `adminReingestDocument`, `adminPreflightUploads`, `adminReserveUpload`, `adminUploadContent`, `adminSubmitUploads` |
| `document_not_found` | 404 | Document not found | The document wasn't found in this index. | no | – | `adminGetDocument`, `adminGetDocumentPdf`, `adminDeleteDocument`, `adminReingestDocument` |
| `version_not_found` | 404 | Upload not found | The upload wasn't found. Restart the upload. | no | – | `adminUploadContent` |
| `run_not_found` | 404 | Ingestion run not found | The ingestion run wasn't found. | no | – | `adminGetIngestionRun` |
| `session_not_found` | 404 | Conversation not found | This conversation isn't available. Start a new chat. | no | – | `createChatTurn`, `getSession`, `listSessionTurns` |
| `turn_not_found` | 404 | Answer not found | We couldn't find that answer in your conversations. | no | – | `putTurnFeedback` |
| `memory_fact_not_found` | 404 | Memory item not found | That memory item doesn't exist. Refresh to see your current memory. | no | – | `updateMemoryFact`, `deleteMemoryFact` |
| `citation_not_found` | 404 | Source not found | This source isn't available. | no | – | `getCitation`, `getCitationContent`, `getCitationDocument` |
| `method_not_allowed` | 405 | Method not allowed | This address doesn't support that action. | no | – | routing (known path, other method) |
| `project_name_taken` | 409 | Name in use | Another project already uses this name. | no | – | `adminCreateProject`, `adminUpdateProject` |
| `project_archived` | 409 | Project archived | This project is archived. Restore it before changing it. | no | – | `adminUpdateProject`, `adminAddProjectMember`, `adminRemoveProjectMember`, `adminMapProjectIndex`, `adminUnmapProjectIndex` |
| `default_index_required` | 409 | Default index needed | A project with indexes needs a default index. Make another index the default instead. | no | – | `adminMapProjectIndex` |
| `default_index_in_use` | 409 | Default index in use | This is the project's default index. Make another index the default before removing it. | no | – | `adminUnmapProjectIndex` |
| `index_not_mappable` | 409 | Index unavailable | This index can't be connected to a project in its current state. | no | – | `adminMapProjectIndex` |
| `project_index_unavailable` | 409 | Project not ready | This project has nothing to search yet. Ask an administrator to connect an index. | no | – | `createChatTurn` |
| `display_name_taken` | 409 | Index name in use | Another index already uses this name. Choose a different name. | no | – | `adminCreateIndex`, `adminUpdateIndex` |
| `index_busy` | 409 | Index busy | This index is busy with another operation. Retry when it finishes. | yes, once the running operation ends | – | `adminUpdateIndex`, `adminDeleteIndex`, `adminStartReingest`, `adminDeleteDocument`, `adminReingestDocument` |
| `index_mapped` | 409 | Index used by projects | Remove this index from every project before deleting it. | no | – | `adminDeleteIndex` |
| `index_not_accepting_uploads` | 409 | Uploads not accepted | This index can't take new documents right now. | no | – | `adminPreflightUploads`, `adminReserveUpload`, `adminUploadContent`, `adminSubmitUploads` |
| `search_index_capacity` | 409 | No room for another index | The search service has no room for another index. Delete an unused index or ask an operator to add capacity. | no | – | `adminCreateIndex`, `adminStartReingest` |
| `no_current_version` | 409 | No ingested version | This document has no ingested version to reprocess. Upload it instead. | no | – | `adminReingestDocument` |
| `content_unchanged` | 409 | File unchanged | This file is already in the index with the same content. | no | – | `adminReserveUpload`, `adminUploadContent` |
| `duplicate_content` | 409 | Duplicate file | The same file is already in this index under another name. | no | – | `adminReserveUpload`, `adminUploadContent` |
| `update_not_confirmed` | 409 | Update not confirmed | This document already exists. Confirm the update to replace it. | no | – | `adminReserveUpload` |
| `version_not_uploading` | 409 | Upload not open | This upload isn't waiting for a file. Restart the upload. | no | – | `adminUploadContent` |
| `turn_in_progress` | 409 | Answer still being written | An answer is still being written in this conversation. Wait for it to finish, then ask your question. | yes, after ra | `retryAfterSeconds` (the whole seconds, rounded up and at least 1, until the running turn's `deadlineAt`), `sessionId`, `turnId` | `createChatTurn` |
| `turn_already_ended` | 409 | Request already finished | That request already finished without a complete answer. Resend the question to retry. | yes, with a new `clientTurnId` | `sessionId`, `turnId` | `createChatTurn` |
| `client_turn_id_conflict` | 409 | Request identifier reused | This request reused an identifier from a different question. Reload the page and retry. | no | `sessionId`, `turnId` | `createChatTurn` |
| `session_turn_limit` | 409 | Conversation limit reached | This conversation has reached its length limit. Start a new chat to continue. | no | `sessionId` | `createChatTurn` |
| `feedback_not_allowed` | 409 | Feedback not available | Feedback can only be given on an answer. | no | – | `putTurnFeedback` |
| `document_pdf_unavailable` | 409 | Full document unavailable | This document is too large to download as one file. Its cited pages still open. | no | – | `getCitationDocument`, `adminGetDocumentPdf`: the version has no whole-document rendition (`12-chat-pipeline.md` section 16.2 step 7) |
| `document_changed` | 410 | Document changed | This document has changed since the answer was written. Ask the question now for an answer from the current version. | no | – | `getCitation`, `getCitationContent`, `getCitationDocument` |
| `content_length_required` | 411 | File size missing | The upload must state its size. | no | – | `adminUploadContent` |
| `precondition_failed` | 412 | Changed by someone else | Someone else changed this item. Reload it to see the latest version, then retry. | after a refetch | – | the 14 `If-Match` operations |
| `request_too_large` | 413 | Request too large | The request is too large. | no | – | every operation with a JSON body |
| `file_too_large` | 413 | File too large | The file is larger than the allowed size. | no | – | `adminUploadContent` (its only status; an oversize reservation is `422 validation_failed`) |
| `unsupported_media_type` | 415 | Unsupported format | This request was sent in a format this operation doesn't accept. | no | – | every operation with a request body, `adminUploadContent` included |
| `validation_failed` | 422 | Invalid input | Some of the information sent isn't valid. | no | `errors` | every operation with a parameter or a body |
| `method_not_found` | 422 | Unknown method | The selected ingestion method isn't available. | no | `errors` | `adminCreateIndex`, `adminUpdateIndex` |
| `method_inactive` | 422 | Method not available | The selected ingestion method isn't available. | no | `errors` | `adminCreateIndex`, `adminUpdateIndex` |
| `invalid_method_parameters` | 422 | Invalid method settings | Some method settings aren't valid. Check the highlighted fields. | no | `errors` | `adminCreateIndex`, `adminUpdateIndex` |
| `unsupported_embedding_model` | 422 | Embedding model not available | The selected option isn't available. | no | `errors` | `adminCreateIndex`, `adminUpdateIndex` |
| `unsupported_dimensions` | 422 | Dimensions not available | The selected option isn't available. | no | `errors` | `adminCreateIndex`, `adminUpdateIndex` |
| `unsupported_analyzer` | 422 | Language analyzer not available | The selected option isn't available. | no | `errors` | `adminCreateIndex`, `adminUpdateIndex` |
| `empty_update` | 422 | Nothing to change | Change at least one setting. | no | `errors` | `adminUpdateIndex` |
| `batch_too_large` | 422 | Too many files | Send at most {maxFilesPerBatch} files at a time. (`{maxFilesPerBatch}` is the value of `DOCCHAT_UPLOAD_MAX_FILES_PER_BATCH`) | no | `errors` | `adminPreflightUploads`, `adminSubmitUploads` |
| `invalid_file_name` | 422 | Invalid file name | Only non-empty PDF, Word (.docx) and PowerPoint (.pptx) files with valid names can be uploaded. | no | `errors` | `adminReserveUpload` |
| `unsupported_type` | 422 | File type not accepted | Only non-empty PDF, Word (.docx) and PowerPoint (.pptx) files with valid names can be uploaded. | no | `errors` | `adminReserveUpload` |
| `empty_file` | 422 | Empty file | Only non-empty PDF, Word (.docx) and PowerPoint (.pptx) files with valid names can be uploaded. | no | `errors` | `adminReserveUpload` |
| `project_not_mapped` | 422 | Project doesn't use this index | Choose a project that uses this index. | no | `errors` | `adminReserveUpload` |
| `size_mismatch` | 422 | File size changed | The file's size changed during upload. Retry the upload. | yes | `errors` | `adminUploadContent` |
| `file_signature_mismatch` | 422 | Content doesn't match file type | The file's content doesn't match its type. | no | `errors` | `adminUploadContent` |
| `hash_mismatch` | 422 | File changed after checking | The file changed after it was checked. Select the file and upload it. | yes | `errors` | `adminUploadContent` |
| `precondition_required` | 428 | Version needed | This change needs the version of the item you're editing. Reload it and retry. | after a refetch | – | the 14 `If-Match` operations |
| `rate_limited` | 429 | Request limit reached | You're sending requests too quickly. Wait a moment, then retry. | yes, after ra | `retryAfterSeconds` 1 to 120 | `createChatTurn`, `getCitationContent`, `getCitationDocument`, `adminGetDocumentPdf` |
| `too_many_streams` | 429 | Too many answers in progress | You already have answers being written. Wait for one to finish, then retry. | yes, after ra | `retryAfterSeconds` 10 | `createChatTurn` |
| `too_many_downloads` | 429 | Too many sources loading | Too many sources are loading at once. Wait for one to finish, then retry. | yes, after ra | `retryAfterSeconds` 10 | `getCitationContent`, `getCitationDocument`, `adminGetDocumentPdf` |
| `token_quota_exceeded` | 429 | Usage limit reached | You've reached your usage limit for now. You can ask more after the time shown. | yes, after ra | `retryAfterSeconds`: the seconds, rounded up, to the next UTC midnight (day quota) or the first instant of the next UTC month (month quota), the larger when both are exceeded | `createChatTurn` |
| `too_many_requests` | 429 | Too many requests | You're sending requests too quickly. Wait a moment, then retry. | yes, after ra | `retryAfterSeconds` 1 | every `/v1` operation except `createChatTurn` |
| `write_rate_limited` | 429 | Too many changes | You're making changes too quickly. Wait a moment, then retry. | yes, after ra | `retryAfterSeconds` 1 to 120 | the 17 write-limited operations |
| `upload_busy` | 429 | Too many uploads | Too many uploads are running. Retrying in a few seconds. | yes, after ra | `retryAfterSeconds` 5 | `adminUploadContent` |
| `internal_error` | 500 | Unexpected error | Something went wrong on our side. Retry, and quote the reference if it keeps happening. | yes | – | every operation except `getHealth` |
| `citation_content_missing` | 500 | Source page unavailable | This source couldn't be loaded. Retry later. | no | – | `getCitationContent`, `getCitationDocument` |
| `document_pdf_missing` | 500 | Document PDF unavailable | The document's PDF couldn't be loaded. Retry later. | no | – | `adminGetDocumentPdf` |
| `authentication_unavailable` | 503 | Sign-in check unavailable | We can't check your sign-in right now. Retry in a minute. | yes, after ra | `retryAfterSeconds` 30 | every operation except `getHealth` |
| `dependency_unavailable` | 503 | Service unavailable | A service DocChat depends on isn't responding. Retry in a few seconds. | yes, after ra | `retryAfterSeconds` 5 | every operation except `getHealth`: SQL or Cosmos DB unreachable, timed out or throttled after the data layer's or SDK's retries (the write check included); on `updateMemoryFact` also fact screening unavailable after its retry |
| `storage_unavailable` | 503 | File storage unavailable | The file couldn't be stored. Retry the upload. | yes | – | `adminUploadContent` |
| `malware_scanning_not_ready` | 503 | Uploads paused | Uploads are paused until malware scanning confirms it's working. Retry later. | yes, after ra | `retryAfterSeconds` 3,600 | `adminPreflightUploads`, `adminReserveUpload`, `adminUploadContent`, `adminSubmitUploads` |

### 2.3 Stream-only codes

These codes reach the client only as the `error` event of a `createChatTurn` stream after its `200` (section 3.3.3); the turn is stored `failed` with `error.code` equal to the event's code. The event carries `message` (the text below) and no title. `internal_error` is the problem code of section 2.2 used as a stream code too, with the same text; the other 13 have `status` `None` in the catalogue. When each one is raised is `12-chat-pipeline.md` section 14.

| Code | Title | `message` | `retryable` | `retryAfterSeconds` |
|---|---|---|---|---|
| `conversation_unavailable` | Conversation unavailable | Your conversation couldn't be loaded. Retry in a moment. | `true` | `null` |
| `retrieval_unavailable` | Document search unavailable | The document search isn't responding. Retry in a moment. | `true` | `null` |
| `source_screening_unavailable` | Source safety check unavailable | The safety check for sources isn't available right now. Retry in a moment. | `true` | `null` |
| `prompt_too_large` | Conversation too long | This conversation is too long to continue. Start a new chat. | `false` | `null` |
| `question_filtered` | Question blocked | This question was blocked by the content safety filter. Rephrase it, then send it. | `false` | `null` |
| `answer_filtered` | Answer blocked | The answer was stopped by the content safety filter. Try asking in a different way. | `false` | `null` |
| `model_busy` | Answer service busy | The answer service is busy. Retry shortly. | `true` | from the model service's retry header, at least 1 and at most 60 |
| `model_unavailable` | Answer service unavailable | The answer service isn't responding. Retry in a moment. | `true` | `null` |
| `model_request_rejected` | Request not processed | The answer service couldn't process this request. Try a shorter question. | `false` | `null` |
| `answer_incomplete` | Answer incomplete | The answer couldn't be completed. Retry or narrow the question. | `true` | `null` |
| `turn_timeout` | Answer took too long | The answer took too long. Retry the question. | `true` | `null` |
| `turn_save_failed` | Answer not saved | The answer was shown but couldn't be saved to your history. Resend the question if you need it saved. | `true` | `null` |
| `service_restarting` | Service restarting | The service is restarting. Resend your question in a moment. | `true` | 5 |
| `internal_error` | Unexpected error | Something went wrong on our side. Retry, and quote the reference if it keeps happening. | `true` | `null` |

### 2.4 Stored-only turn code

| Code | Title | Text | Retryable | Where |
|---|---|---|---|---|
| `turn_abandoned` | Answer interrupted | This answer was interrupted before it finished. | yes, with a new `clientTurnId` | the stored `error.code` of a turn that passed its `deadlineAt` without being finalised (its process died), and `TurnView.errorCode` of such a turn; never a problem and never a stream event (`13-conversations-and-memory.md` section 2) |

### 2.5 Validation reasons

Every `422` carries at least one `errors[]` entry whose `reason` is one of these:

| Reason | Meaning | Used by |
|---|---|---|
| `required` | a required field, parameter, header or body is missing | every model |
| `unknown_field` | a body field or query parameter that isn't in the model, or a body sent where none is accepted | every model |
| `invalid_type` | the wrong JSON type | every model |
| `invalid_json` | the body isn't JSON, or isn't UTF-8 | every JSON-body operation |
| `invalid_uuid` | not a canonical UUID | every identifier |
| `invalid_format` | a pattern mismatch (for example a SHA-256 that isn't 64 hexadecimal characters, a file extension that isn't accepted, content that doesn't match its type or hash) | every model; the upload checks |
| `invalid_cursor` | a cursor that fails to decode, fails its model or doesn't match the request's filters | every list operation |
| `invalid_characters` | a code point of category Cc, Cf, Cs, Co or Cn in a text type (LF and TAB allowed in multi-line text; the chat question allows LF and TAB and refuses other Cc code points and surrogates) | the text types, the question, file names |
| `too_short` | below the minimum length or item count | every model |
| `too_long` | above the maximum length | every model |
| `out_of_range` | a number outside its range, or a declared size that doesn't fit | every model; `body.sizeBytes`, `header.Content-Length` |
| `too_many_items` | a list above its maximum | every model; `batch_too_large` |
| `not_allowed` | a value not allowed here or in this combination | enumerations; `body.status` in `adminUpdateProject`; the method, model, dimension, analyzer and project checks |
| `duplicate` | a duplicate list entry | `exampleQuestions`, `initialMemberIds`, `versionIds`, `ocrLanguages` |
| `not_found` | a referenced identifier has no row | `body.initialMemberIds.<i>`, `body.methodKey` |
| `category_requires_down` | a feedback category with rating `up` | `putTurnFeedback` |
| `comments_disabled` | a feedback comment while `feedback.comments_enabled` is false | `putTurnFeedback` |
| `empty` | nothing is left after normalisation, or a question holds no code point of category L, N, P or S | `createChatTurn`, `updateMemoryFact` |
| `contains_link` | a memory fact holds a URL, host path, IP address or e-mail address | `updateMemoryFact` |
| `contains_code` | a memory fact holds code or markup | `updateMemoryFact` |
| `contains_instruction` | a memory fact reads as an instruction to the assistant (a pattern match, or a Prompt Shields detection on changed text) | `updateMemoryFact` |
| `contains_secret` | a memory fact looks like a credential or a long number | `updateMemoryFact` |
| `duplicate_fact` | a memory fact equals or nearly equals another fact | `updateMemoryFact` |
| `memory_full` | the facts' token total would exceed `memory.max_tokens` | `updateMemoryFact` |

### 2.6 Codes carried as data

- `VersionSummary.failureCode`, `RunItem.outcomeCode` and `RunItem.warnings[].code` carry the ingestion item codes (failures, skips, cancellations and warnings) of `14-ingestion.md` section 17; `VersionSummary.failureMessage` and `RunItem.outcomeMessage` carry each code's safe message from that catalogue. They are data, never problem codes.
- `TurnView.errorCode` carries a code of section 2.3 or `turn_abandoned`.
- The internal codes stored in conversation documents (`model_unavailable`, `content_filtered`, `invalid_output`, `store_conflict`) and the ingestion activity result `lease_lost` never leave the server.

## 3. Operations

Each operation below states: its **access** level and authorisation rule; its **scoped parameters** (`x-docchat-scoped-params`) with the answer each gives for an identifier the caller can't reach; its **route options** where they differ from the defaults; its **request** (path, query and header parameters, body model and request example); its **responses** with their models; its **error codes** (the common sets of section 1.14 and its own codes, which together are its `x-docchat-error-codes`); the **order** of its checks where it goes beyond section 1.3; its **limits**; its **idempotency and concurrency**; and its **effects** (writes, audit rows, events). Models used by more than one operation are in section 4. Every path parameter named `…Id` is a UUID (section 1.7); a malformed one answers `422 validation_failed` with reason `invalid_uuid`.

### 3.1 Public

#### 3.1.1 `getHealth` — `GET /healthz`

- **Access:** `public` (`x-docchat-access: public`, `security: []`). No in-app check of any kind runs: no token validation, request limit, provisioning or ops config. In deployed environments platform authentication still sits in front of it (no excluded path), so App Service's health check passes through the platform and an anonymous external call gets the platform's `401` (ADR 0023).
- **Scoped parameters:** none.
- **Request:** no parameters, no body.
- **Responses:** `200` with an empty body (`Content-Length: 0`) and the headers every response carries (section 1.10).
- **Error codes:** none (`x-docchat-error-codes` is empty); another method on the path gets the router's `405 method_not_allowed`.
- **Behaviour:** it reads no data, calls no dependency and writes no log record (its server span is still exported). It answers only once the lifespan start-up has completed, because Uvicorn serves only after start-up, and a process whose settings fail validation exits before serving, so a misconfigured instance never reports healthy. It is liveness only: a dependency failure isn't an instance failure, and taking every instance out of rotation during a short SQL outage would turn a partial outage into a total one. App Service calls it as the health check (`healthCheckPath: '/healthz'`, `83-infrastructure.md` section 2); availability and latency queries exclude it (`85-observability.md` section 5).
- **Limits:** none. **Idempotency:** a pure read.

### 3.2 Current user and projects

#### 3.2.1 `getCurrentUser` — `GET /v1/me`

- **Access:** `user`. Like every `/v1` route it runs first-sign-in provisioning (below) at step 5, so the caller's `app.users` row exists afterwards.
- **Scoped parameters:** none.
- **Request:** no parameters, no body.
- **Responses:** `200` `CurrentUser`:

| Field | Type | Value |
|---|---|---|
| `userId` | UUID | the token's `oid` |
| `displayName` | string, 0 to 256 | `Principal.display_name` |
| `email` | string, 0 to 320 | `Principal.email` |
| `roles` | array of 1 to 2 of `Admin`, `User`, sorted ascending | the roles the token grants |
| `isAdmin` | boolean | `Admin` in `roles` |
| `features` | `CurrentUserFeatures` | `memoryEnabled` (boolean, the ops-config value `memory.enabled`) and `feedbackComments` (boolean, the ops-config value `feedback.comments_enabled`) |

Identity values come from the validated token, never from the stored row, so a role change shows as soon as the client holds a new token; `features` comes from the request's ops-config snapshot, so a change shows within 60 seconds. The API registers the `memory` and `feedback` ops-config areas at start-up, so both keys are always present.
- **Error codes:** Base, Burst.
- **Limits:** the burst limit. **Idempotency:** a read; its only side effect is provisioning or the last-seen update.
- **Cost:** with the caller's last-seen write under 15 minutes old it makes no SQL call apart from the lazy ops-config refresh (at most one per process every 60 seconds), so it is the cheap authenticated call of the load test (`84-testing.md` section 7: p95 at most 200 ms beside 100 streams).

**First-sign-in provisioning** (`docchat_core.users.ensure_user(principal)`, step 5 of every `/v1` route; ADR 0021). It is skipped when this process's throttle map holds the caller's `oid` with a time less than 15 minutes old (a map keyed by `oid`, at most 50,000 entries, the oldest evicted first). Otherwise it runs one transaction (`SET XACT_ABORT ON`):

```sql
UPDATE app.users WITH (UPDLOCK, HOLDLOCK)
SET tenant_id = :tid,
    display_name = CASE WHEN :display_name = N'' THEN display_name ELSE :display_name END,
    email = CASE WHEN :email = N'' THEN email ELSE :email END,
    last_known_role = :role,
    last_seen_at = CASE WHEN first_seen_at > SYSUTCDATETIME() THEN first_seen_at ELSE SYSUTCDATETIME() END
WHERE object_id = :oid;
```

When that statement touched no row:

```sql
INSERT INTO app.users (object_id, tenant_id, display_name, email, last_known_role)
VALUES (:oid, :tid, :display_name, :email, :role);

INSERT INTO app.project_members (user_object_id, project_id, grant_source, granted_by)
SELECT :oid, p.id, N'default', NULL
FROM app.projects AS p
WHERE p.is_default = 1;

INSERT INTO app.audit_events
    (actor_type, actor_object_id, subject_object_id, action, target_type, target_id, metadata_json, correlation_id)
VALUES (N'user', :oid, :oid, N'user.provisioned', N'user', :oid_text,
        :metadata_json, :correlation_id);   -- {"defaultProjectCount": <rows inserted above>}
```

Then commit and record the `oid` in the throttle map. `:role` is `Admin` when the token's roles contain `Admin`, else `User` (display only). `HOLDLOCK` takes a key-range lock, so two first requests of one user serialise and the second updates instead of inserting. SQL Server reads `SYSUTCDATETIME()` once, when a statement starts, so the second request's `UPDATE` can carry a time from before it waited for the first request's insert; the `CASE` keeps `last_seen_at` at or after `first_seen_at`, as `CK_users_seen_order` (`80-data-model.md` section 2) requires (found by T-012 on 2026-11-08). SQL error 1205 (deadlock), 2627 or 2601 (duplicate key) rolls back and retries the whole transaction once; a second failure, or SQL unreachable after the data layer's retries, answers `503 dependency_unavailable` and leaves the throttle map unchanged. Default projects are always active (`80-data-model.md` section 2), so a new user's default memberships are usable at once; flagging a project default later never adds memberships for existing users. The custom event `user_provisioned` and the metric `docchat.users.provisioned` record a provisioning.

#### 3.2.2 `listMyProjects` — `GET /v1/projects`

- **Access:** `user`. The items are the projects the caller can reach now (section 1.4.2): every active project for an administrator; the active projects with a membership row for anyone else.
- **Scoped parameters:** none.
- **Request:** query `limit` (integer 1 to 100, default 50), `cursor` (section 1.8).
- **Responses:** `200` `ProjectSummaryPage` (`items` of `ProjectSummary`, section 4.4; `nextCursor`), ordered by `name`.
- **Error codes:** Base, Burst, Params (`422` reasons `out_of_range`, `invalid_cursor`, `unknown_field`).
- **Behaviour:** one index-backed query (Q5, section 1.4.4), which also seeks the default mapping, its logical index and that index's active physical index for `hasDefaultIndex`. An empty first page is the web client's No projects state (`10-web-client.md` section 4).
- **Limits:** the burst limit. **Idempotency:** a pure read.

### 3.3 Chat

#### 3.3.1 `createChatTurn` — `POST /v1/chat/turns`

- **Access:** `user`; then `Admin` or a membership of the project, which must be `active` (`authorize_project_for_chat`); when `sessionId` is given, a session in the caller's own partition whose `projectId` equals the request's.
- **Scoped parameters:** `body:/projectId` → `404 project_not_found`; `body:/sessionId` → `404 session_not_found`.
- **Route options:** `body_limit=32768`, `burst_limited=False`, `write_limited=False`; content type `application/json`.
- **Request:** `Content-Type: application/json`; the API always answers a stream and doesn't negotiate on `Accept` (the web client sends `Accept: application/x-ndjson`). Body `ChatTurnRequest` (`extra="forbid"`; no index, key, model, prompt, option or history is accepted):

| Field | Type | Required | Rules |
|---|---|---|---|
| `projectId` | UUID | yes | canonical UUID, lower-cased |
| `sessionId` | UUID or `null` | no (default `null`) | absent or `null` starts a new session |
| `clientTurnId` | UUID | yes | one per question the user sends; a retry uses a new one |
| `question` | string | yes | normalised first: CR LF and lone CR become LF, Unicode NFC, leading and trailing whitespace removed; then 1 to 4,000 code points, no code point of category Cc other than LF and TAB, no surrogate (category Cs), and at least one code point of category L, N, P or S. Refusals: `422 validation_failed` with one entry at `body.question` and reason `empty` (nothing left, or no code point of L, N, P or S), `too_long` or `invalid_characters` |

Request example: `{"projectId": "3f2b8c1e-7d4a-4e9b-9c55-0a6f1d2e8b74", "sessionId": null, "clientTurnId": "b8e0d6a2-51c3-4f7e-8a19-6c2d4e9f0a13", "question": "How many days do I have to submit an expense claim?"}`.
- **Responses:** `200` with `Content-Type: application/x-ndjson`, `Cache-Control: no-store`, `X-Content-Type-Options: nosniff` and the other headers of section 1.10; chunked (no `Content-Length`); never `Content-Encoding`. The body is a stream of `ChatStreamEvent` lines (sections 3.3.2 and 3.3.3).
- **Error codes** (problems, before the stream starts): Base, Body, and `404 project_not_found`, `404 session_not_found`, `409 project_index_unavailable`, `409 turn_in_progress`, `409 turn_already_ended`, `409 client_turn_id_conflict`, `409 session_turn_limit`, `429 rate_limited`, `429 too_many_streams`, `429 token_quota_exceeded`. Here `503 dependency_unavailable` also covers a `usage` operation timing out twice (2 s per point read, 3 s per write) and three session ETag conflicts without an active turn. No Burst or Write set. After the `200`, failures are `error` events with the codes of section 2.3.
- **Order:** the chat steps of section 1.3.
- **Turn creation** (chat step 8, `ConversationStore.begin_turn`, ADR 0056; `13-conversations-and-memory.md` sections 2 and 3):
  - A new session's ID is the UUID version 5 over the namespace `60191fe1-b79c-4c47-995d-e0a0e20cb164` and the name `"{oid}:{clientTurnId}"`; a turn's ID is the UUID version 5 over the namespace `fc6c167c-bdc0-407d-9fcb-0bb4c6a2c701` and the name `"{sessionId}:{clientTurnId}"` (lower-case UUID text in both names). The turn, and for a new session the session, are created in one Cosmos DB transactional batch in the partition `[oid, sessionId]`, the turn in state `streaming`.
  - An existing session is point-read from `[oid, sessionId]`; a missing document and a session of another project both answer `404 session_not_found` with the same body.
  - One streaming turn per session: a turn starts only when the session's `activeTurn` is `null` or past its `deadlineAt` (the stale turn is then marked `failed` with `turn_abandoned` in the same batch); otherwise `409 turn_in_progress`. `deadlineAt` is the request's receipt time + `chat.max_turn_seconds` + 30 s. Concurrent starts are serialised by the session's ETag: 3 attempts, then `409 turn_in_progress` when the last read shows a live `activeTurn`, otherwise `503 dependency_unavailable`.
  - A session holding `conversations.max_turns_per_session` turns (default 500, range 50 to 5,000) answers `409 session_turn_limit`, which carries `sessionId` and no `turnId`, because no turn is created.
  - A duplicate `clientTurnId` (the derived turn ID exists) with the same question and project: `completed` → a replay (section 3.3.4); `streaming` before its `deadlineAt` → `409 turn_in_progress`; `streaming` past it (then marked `failed` with `turn_abandoned`), `failed` or `cancelled` → `409 turn_already_ended`; with a different question or project → `409 client_turn_id_conflict`.
- **Limits:** section 1.11 (`createChatTurn` row). Every request that passes token and body validation counts toward the request rate, replays and requests later refused with `404` or `409` included; a request refused by the rate check isn't counted. A stream holds one lease from chat step 5 until its `citations` event is written (or its end, when it ends before citations); a replay holds one too. Body at most 32,768 bytes.
- **Idempotency and concurrency:** section 1.12; one streaming turn per session.
- **Effects:** the turn and session documents (`13-conversations-and-memory.md` section 1); a new session's title, derived from its first question (`13-conversations-and-memory.md` section 5); token usage on the user's day and month counters (`12-chat-pipeline.md` section 15); after a `completed` turn, the background summary and memory work (`13-conversations-and-memory.md` sections 6 and 7).

#### 3.3.2 Stream events

`ChatStreamEvent` is the union of eight models discriminated by `type`. Every field is always present; a nullable field is sent as `null`; names are camelCase. No event carries retrieved text, prompts, scores, deployment names, content-filter annotations or document, version, chunk-set or chunk identifiers. When each event is written is `12-chat-pipeline.md` section 14.

| Model | `type` | Fields (type, limits) |
|---|---|---|
| `StreamMetaEvent` | `meta` | `sessionId` (UUID); `turnId` (UUID); `sessionCreated` (boolean, true when this request created the session); `replayed` (boolean); `correlationId` (string, `^[0-9a-f]{32}$`, this request's trace ID) |
| `StreamStatusEvent` | `status` | `status` (enum with the one value `search_broadened`) |
| `StreamDeltaEvent` | `delta` | `text` (string, 1 to 4,000 characters of answer text, holding zero or more citation tokens: U+27E6, the decimal citation number 1 to 16, U+27E7, for example `⟦2⟧`; only the API mints these tokens, ADR 0064) |
| `StreamCitationsEvent` | `citations` | `citations` (array of 0 to 16 `Citation`, section 4.5, in number order); `allowedLinks` (array of 0 to 50 strings, each an `https` URL of at most 2,048 characters) |
| `StreamFollowupsEvent` | `followups` | `questions` (array of 1 to 3 strings, each 1 to 200 characters) |
| `StreamHeartbeatEvent` | `heartbeat` | none beyond `type` |
| `StreamErrorEvent` | `error` | `code` (enum of the 14 codes of section 2.3); `message` (string, 1 to 300 characters, the code's text); `retryable` (boolean); `retryAfterSeconds` (integer 1 to 60, or `null`); `correlationId` (string, `^[0-9a-f]{32}$`) |
| `StreamDoneEvent` | `done` | `turnId` (UUID); `truncated` (boolean, true when the answer stopped at the completion cap); `usage` (`TurnUsageSummary`, section 4.5, over every model call of the turn so far) |

Illustrative stream (identifiers shortened):

```text
{"type":"meta","sessionId":"5f0c…","turnId":"9a1e…","sessionCreated":true,"replayed":false,"correlationId":"0af7651916cd43dd8448eb211c80319c"}
{"type":"delta","text":"Expense claims are submitted within 30 days of purchase ⟦1⟧, "}
{"type":"delta","text":"with receipts attached for items over the stated limit ⟦2⟧."}
{"type":"citations","citations":[{"number":1,"title":"Expenses guide","pageNumber":3,"headingPath":["Claims","Deadlines"],"contentType":"application/pdf","openAs":"pdf_page"},{"number":2,"title":"Finance handbook","pageNumber":12,"headingPath":["Receipts"],"contentType":"application/vnd.openxmlformats-officedocument.wordprocessingml.document","openAs":"pdf_page"}],"allowedLinks":[]}
{"type":"followups","questions":["What is the receipt limit?","Who approves late claims?"]}
{"type":"done","turnId":"9a1e…","truncated":false,"usage":{"promptTokens":9120,"completionTokens":640,"reasoningTokens":212,"cachedTokens":0,"totalTokens":9760,"estimated":false}}
```

#### 3.3.3 Framing, order and the terminal event

- **Framing.** Each event is one JSON object serialised compactly with Pydantic `model_dump_json(by_alias=True)` (UTF-8, non-ASCII characters unescaped) and followed by exactly one `\n` (0x0A); JSON string escaping keeps raw newlines out of every event; there are no blank lines and no byte-order mark. The route returns Starlette's `StreamingResponse(media_type="application/x-ndjson")`, because FastAPI's built-in JSON Lines streaming sends another media type (ADR 0055).
- **Order.** `meta` is always the first line. Then at most one `status`, before any `delta`; zero or more `delta`; exactly one `citations` on success, written even when both arrays are empty; at most one `followups`; then `done`. `error` is valid at any point after `meta` and ends the stream. `heartbeat` is valid anywhere after `meta` and before the terminal event. Every stream ends with exactly one `done` or `error`, and nothing follows it. The stream writer enforces this order as a state machine: a write out of order is a programming error, logged, and turned into `error` `internal_error` while no terminal event has been written. A stream that ends without a terminal event (a cut connection, a crashed process) is a failure the web client shows.
- **Heartbeat.** Whenever 15 seconds pass without a line, the writer writes `{"type":"heartbeat"}`; the count restarts after every line, heartbeats included. The value is a protocol constant, not a setting.
- **Persistence behind the wire order.** The turn is finalised as `completed` as soon as the model stream ends, before `citations` is written, so the session is free for the next question when the client receives `citations`. A failed save ends the stream with `error` `turn_save_failed` and no `citations`. The `followups` event is written only after the follow-ups are stored on the turn.
- **Outcomes.** A completed answer ends with `done`; an answer that stopped at the completion cap with visible text ends with `done` and `truncated: true`; one with no visible text ends with `error` `answer_incomplete`. Every other failure after the `200` ends with the `error` event of its code (section 2.3), and the turn is stored `failed` with that code. A failure is never answered with substitute text.

#### 3.3.4 Replay, disconnects, the deadline and shutdown

- **Replay.** A duplicate `clientTurnId` of a `completed` turn gets the same `200` stream shape built from the stored turn: `meta` with `sessionCreated: false`, `replayed: true` and this request's `correlationId`; the stored answer as `delta` events of at most 2,000 characters each, cut on code-point boundaries and never inside a citation token; `citations` with the stored citations and `allowedLinks`; `followups` when the turn has stored follow-ups; then `done` with `usage` projected from the stored totals and `truncated` equal to the stored `answerTruncated`. A replay makes no model call, records no usage and writes nothing.
- **Disconnects.** When the client disconnects before the model stream ends, the API cancels every in-flight call of the turn within 1 second and saves the turn `cancelled` with the text released so far; no follow-ups and no after-response work follow. Once the model stream has ended, the turn stays `completed` whatever the connection does; a running follow-ups call is cancelled and no further event is written. A disconnect during a replay changes nothing.
- **Deadline.** `chat.max_turn_seconds` (default 180, range 60 to 220) runs from the route's receipt of the request. When it passes before finalisation, in-flight calls are cancelled, the turn is saved `failed` with the text released so far, and the stream ends with `error` `turn_timeout`; when it passes after finalisation, it only cancels the follow-ups call and the stream ends with `done`.
- **Shutdown.** A turn still running 90 seconds after the instance's stop signal is cancelled: an open stream ends with `error` `service_restarting` (`retryAfterSeconds` 5) and the turn is saved `failed` with that code; a turn already finalised `completed` drops its follow-ups and ends with `done` (section 1.15; `12-chat-pipeline.md` section 14).

### 3.4 Sessions and feedback

Session and turn reads and feedback run against the caller's own partition of the container `conversations` (`13-conversations-and-memory.md` sections 1 and 4). Every Cosmos DB operation here uses the per-operation timeouts of that file (2 s per point read, 5 s per query page, batch, patch, replace and create); a store that stays unreachable or throttled answers `503 dependency_unavailable`.

#### 3.4.1 `listSessions` — `GET /v1/sessions`

- **Access:** `user`. The caller's own sessions only, in projects the caller can reach now; hidden sessions never appear (ADR 0078).
- **Scoped parameters:** `projectId` (query) → `404 project_not_found`.
- **Request:** query `limit` (integer 1 to 50, default 20), `cursor` (section 1.8), `projectId` (UUID, optional; narrows the list to one reachable project).
- **Responses:** `200` `SessionPage` (`items` of `SessionSummary`, section 4.5; `nextCursor`), ordered by `lastTurnAt` descending, then `sessionId` descending.
- **Error codes:** Base, Burst, Params (`out_of_range`, `invalid_cursor`, `invalid_uuid`, `unknown_field`); `404 project_not_found`.
- **Order and rules:** (1) validate the query and decode the cursor; (2) `reachable` = `list_reachable_project_ids(principal)`; with `projectId`, a project outside `reachable` answers `404 project_not_found` and the filter is that one project; without it the filter is `reachable`, sorted; (3) an empty filter answers `{"items": [], "nextCursor": null}` without querying Cosmos DB; (4) otherwise one query over the caller's partition prefix `[oid]`, filtered by the project list (at most 2,000 project IDs), reading `limit + 1` items (the statement is `13-conversations-and-memory.md` section 4).
- **Limits:** the burst limit. **Idempotency:** a pure read; a session that receives a new turn while its owner pages moves to the top and can appear on two pages, and the client de-duplicates by `sessionId`.

#### 3.4.2 `getSession` — `GET /v1/sessions/{sessionId}`

- **Access:** `user`. The session must exist in the caller's partition `[oid, sessionId]` and its stored `projectId` must be reachable now; a missing session, another user's session ID and a hidden session all answer `404 session_not_found` with the same body.
- **Scoped parameters:** `sessionId` → `404 session_not_found`.
- **Request:** path `sessionId`; query `turnsLimit` (integer 1 to 50, default 20).
- **Responses:** `200` `SessionDetail`:

| Field | Type | Value |
|---|---|---|
| `sessionId` | UUID | |
| `projectId` | UUID | the session's project; never changes |
| `title` | string, 1 to 80 | derived from the first question when the session was created; never changes |
| `createdAt` | time | |
| `lastTurnAt` | time | the creation time of the session's newest turn |
| `turnCount` | integer, 1 to `conversations.max_turns_per_session` | equals the newest turn's `seq` |
| `turns` | array of 0 to 50 `TurnView` (section 4.5) | the newest `turnsLimit` turns, ascending by `seq` |
| `earlierTurnsCursor` | string or `null` | a `listSessionTurns` cursor for the turns before the first one returned; `null` when the oldest turn is included |

- **Error codes:** Base, Burst, Params; `404 session_not_found`.
- **Order and rules:** point-read the session; `authorize_project_for_chat(principal, session.projectId)`, whose refusal answers `404 session_not_found` (a SQL failure answers `503 dependency_unavailable`); one single-partition query for the turns. A turn stored as `streaming` whose `deadlineAt` has passed is shown as `failed` with `errorCode` `turn_abandoned`. Nothing else leaves the server: not document, version, chunk-set or index IDs, chunk keys, the standalone or search query, retrieval results or scores, the trace, the model input, deployments, usage, timings, the broadened flag, the finish reason, memory-extraction state, `clientTurnId`, the trace ID or `deadlineAt`.
- **Limits:** the burst limit; at most 50 turns per response. **Idempotency:** a pure read. The web client calls it every 5,000 ms, for at most 250 s, while the newest turn shows `streaming` (`10-web-client.md` section 8).

#### 3.4.3 `listSessionTurns` — `GET /v1/sessions/{sessionId}/turns`

- **Access:** `user`; as `getSession`.
- **Scoped parameters:** `sessionId` → `404 session_not_found`.
- **Request:** path `sessionId`; query `limit` (integer 1 to 50, default 20), `cursor` (optional; absent returns the newest page; section 1.8).
- **Responses:** `200` `TurnPage` (`items` of `TurnView`, ascending by `seq` within the page; `nextCursor` leading to the next older page, `null` when the oldest turn is included).
- **Error codes:** Base, Burst, Params (`invalid_cursor` also when `b` is outside 2 to `turnCount + 1`); `404 session_not_found`.
- **Order and rules:** as `getSession`, reading turns with `seq` below the cursor's `b` (or below `turnCount + 1` without a cursor).
- **Limits:** the burst limit. **Idempotency:** a pure read.

#### 3.4.4 `putTurnFeedback` — `PUT /v1/sessions/{sessionId}/turns/{turnId}/feedback`

- **Access:** `user`, write-limited. The turn must exist in the caller's partition `[oid, sessionId]` and its project must be reachable now; a missing session, a missing turn and a hidden session all answer `404 turn_not_found` with the same body.
- **Scoped parameters:** `sessionId` → `404 turn_not_found`; `turnId` → `404 turn_not_found`.
- **Request:** body `FeedbackRequest` (`extra="forbid"`):

| Field | Type | Required | Rules |
|---|---|---|---|
| `rating` | `up` or `down` | yes | |
| `category` | `incorrect`, `incomplete`, `not_relevant`, `other`, or `null` | no (default `null`) | non-null only with `rating` `down`, else `422` at `body.category` with reason `category_requires_down` |
| `comment` | string or `null` | no (default `null`) | at most 8,000 code points as sent; then normalised: Unicode NFC, CR LF and lone CR turned into LF, code points of category Cc other than LF and TAB and of categories Cf, Co and Cs removed, trimmed, an empty result becoming `null`; the result at most 2,000 code points (`422` at `body.comment` with `too_long`). A non-null comment while `feedback.comments_enabled` is false answers `422` at `body.comment` with `comments_disabled` |

Request example: `{"rating": "down", "category": "incomplete", "comment": "The answer left out the second approval step."}`.
- **Responses:** `200` `FeedbackView` (section 4.5), sent only after the write commits.
- **Error codes:** Base, Burst, Write, Body; `404 turn_not_found`; `409 feedback_not_allowed`.
- **Order and rules:** (1) body validation and normalisation; (2) point-read the turn and `authorize_project_for_chat` with its `projectId` (`404 turn_not_found`); (3) the turn must be `completed`, or `cancelled` with a non-empty `answer`, else `409 feedback_not_allowed`; (4) `patch_item` with `set /feedback` (`{rating, category, comment, createdAt, updatedAt}`, `createdAt` the stored value or now, `updatedAt` now) and `set /updatedAt`, conditioned on the ETag of the turn just read; a `412` re-reads from step 2 and reapplies the change, at most 3 attempts, then `503 dependency_unavailable`.
- **Limits:** the burst and write limits; body at most 65,536 bytes.
- **Idempotency and concurrency:** no `If-Match`; the last write wins (a `PUT` carries the whole value and only the turn's owner writes it); the same body twice stores the same value with `updatedAt` moved. There is no delete (ADR 0080).
- **Effects:** after the commit, the custom event `turn_feedback_saved` (`rating`, `category` or `none`, `projectId`, `turnId`, `sessionId`, `isChange`, `hasComment`) and the metric `docchat.feedback.saved` (`rating`, `category`); never the comment. No audit row.

### 3.5 Citations

A citation is addressed only by session, turn and number; no document ID, path, container or account ever comes from the client (ADR 0069). The algorithms, the document query and the serving rules are `12-chat-pipeline.md` section 16.

#### 3.5.1 `getCitation` — `GET /v1/sessions/{sessionId}/turns/{turnId}/citations/{citationNumber}`

- **Access:** `user`. The turn is read from the caller's own partition, the project's access is re-checked, and the cited document's logical index must still be mapped to the turn's project.
- **Scoped parameters:** `sessionId`, `turnId` and `citationNumber` → `404 citation_not_found` each.
- **Request:** path `sessionId` (UUID), `turnId` (UUID), `citationNumber` (integer 1 to 16).
- **Responses:** `200` `CitationDetail`:

| Field | Type | Value |
|---|---|---|
| `number` | integer, 1 to 16 | the citation number |
| `title` | string, 1 to 300 | the document title shown for the citation |
| `pageNumber` | integer ≥ 1 | the cited page of the version's PDF, for every format (for a DOCX or PPTX, a page of the PDF converted from it) |
| `headingPath` | array of 0 to 8 strings of 1 to 200 | |
| `contentType` | `application/pdf`, `application/vnd.openxmlformats-officedocument.wordprocessingml.document` or `application/vnd.openxmlformats-officedocument.presentationml.presentation` | the cited version's media type |
| `openAs` | `pdf_page` | every citation opens as the rendition of its page, for every format |
| `fileName` | string, 1 to 255 code points | `<stem>-page-<n>.pdf`, where `<n>` is `pageNumber` and `<stem>` is the document's source name without its extension, cut to 255 code points minus the length of `-page-<n>.pdf` |
| `documentPdfAvailable` | boolean | `true` when the version has a whole-document rendition, so `getCitationDocument` serves it; `false` when the document was too large for one (`14-ingestion.md` section 14.4) |
| `documentFileName` | string, 1 to 255 code points | `<stem>.pdf`, with `<stem>` cut to 251 code points; returned whatever `documentPdfAvailable` holds |

- **Error codes:** Base, Burst, Params; `404 citation_not_found`; `410 document_changed`.
- **Order and rules:** (1) path validation; (2) the turn, point-read in `[oid, sessionId]`: missing, or `status` `streaming`, → `404 citation_not_found`; (3) `authorize_project_for_chat(principal, turn.projectId)`: refused → `404 citation_not_found`; (4) the stored citation with that number, else `404 citation_not_found`; (5) one SQL query joining the document, the cited version, the cited chunk set and the project's mapping of the document's index: no row → `410 document_changed`; the index not mapped to the turn's project → `404 citation_not_found`; the document deleted, or its current version not the cited one, → `410 document_changed`; (6) the cited chunk set's `pdf_sha256` not equal to the version's `sha256` (PDF) or `converted_sha256` (DOCX and PPTX), `NULL` on either side included, → `410 document_changed`, because the renditions then show another PDF than the one the citation's page refers to (`12-chat-pipeline.md` section 16.2). A citation from a `cancelled` or `failed` turn opens like any other.
- **Limits:** the burst limit only; it returns metadata, not file bytes. **Idempotency:** a pure read.

#### 3.5.2 `getCitationContent` — `GET /v1/sessions/{sessionId}/turns/{turnId}/citations/{citationNumber}/content`

- **Access:** `user`; as `getCitation`, re-run in full on every call.
- **Scoped parameters:** `sessionId`, `turnId` and `citationNumber` → `404 citation_not_found` each.
- **Request:** path as `getCitation`.
- **Responses:** `200` with the single-page rendition of the cited page (the citation's `pageNumber`) from the container `renditions`, for every format, always the full body (no range requests), with `Content-Type: application/pdf` and the file name of `getCitation`'s `fileName`.

No uploaded file is ever served, of any format: an uploaded PDF can carry document-level actions, scripts and embedded files, and a Word or PowerPoint file macros, links and embedded objects, which the verified renditions don't (`14-ingestion.md` sections 14 and 15.11). Every file response carries `Content-Length` (the blob size), `Content-Disposition` from `content_disposition(<file name>)` (section 1.10), `Content-Security-Policy: sandbox`, `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer` and `Strict-Transport-Security: max-age=31536000`, and is never compressed.
- **Error codes:** Base, Burst, Params; `404 citation_not_found`; `410 document_changed`; `429 rate_limited`; `429 too_many_downloads`; `500 citation_content_missing`.
- **Order and rules:** (1) path validation; (2) the download request window: `429 rate_limited` with `retryAfterSeconds` (section 1.11); (3) a download lease: `429 too_many_downloads` with `retryAfterSeconds` 10; both fail closed with `503 dependency_unavailable`; (4) steps 2 to 6 of `getCitation`; (5) the blob: a storage `404` for the current version answers `500 citation_content_missing` and logs `citation_blob_missing`; (6) the stream. Every request that passes path validation counts in the window, those later refused with `404` or `410` included. The lease is released (a delete conditioned on its ETag, shielded from cancellation) whenever the response ends for any reason; a `404`, `412` or timeout on that delete is ignored, because its 300-second TTL ends it.
- **Streaming:** bytes are read from the blob in 1 MiB chunks and written to the connection in 64 KiB pieces by the response class `docchat_api.chat.citations.CitationFileResponse` (a `StreamingResponse` subclass), which wraps each piece's send in a 30-second timeout and owns the lease release. A piece not accepted within 30 seconds aborts the response (`slow_client`); a download still running 300 seconds after its lease was acquired is aborted (`duration`); the shutdown drain aborts running downloads (`drain`). An aborted response closes the connection with no problem body (its headers are already sent), logs `citation_download_aborted` and counts `docchat.citation.download_aborts`; a storage error after the headers ends the body early and logs `citation_stream_failed`. The client detects a body shorter than `Content-Length` (`10-web-client.md` section 8).
- **Limits:** section 1.11 (`getCitationContent` and `getCitationDocument` row) and the burst limit (ADR 0148).
- **Idempotency:** a read; each call counts in the download window.

#### 3.5.3 `getCitationDocument` — `GET /v1/sessions/{sessionId}/turns/{turnId}/citations/{citationNumber}/document`

- **Access:** `user`; as `getCitation`, re-run in full on every call.
- **Scoped parameters:** `sessionId`, `turnId` and `citationNumber` → `404 citation_not_found` each.
- **Request:** path as `getCitation`.
- **Responses:** `200` with the whole-document rendition of the cited version (`document.pdf` in the container `renditions`, `14-ingestion.md` section 14.7), for every format, always the full body, with `Content-Type: application/pdf`, `Content-Disposition: attachment; filename*=UTF-8''<encoded>` (with its ASCII `filename` fallback) from `content_disposition(<documentFileName>)`, and every other header of `getCitationContent`. It serves the file the reader views or saves when the whole document is wanted; the uploaded file is never served.
- **Error codes:** Base, Burst, Params; `404 citation_not_found`; `409 document_pdf_unavailable`; `410 document_changed`; `429 rate_limited`; `429 too_many_downloads`; `500 citation_content_missing`.
- **Order and rules:** steps (1) to (4) of `getCitationContent`; then (5) the version's `document_pdf_status`: anything other than `built` answers `409 document_pdf_unavailable` and reads no blob (`12-chat-pipeline.md` section 16.2 step 7); (6) the blob, as `getCitationContent`; (7) the stream. The download window and lease are the ones `getCitationContent` uses: one window and one set of leases per user for both operations.
- **Streaming:** as `getCitationContent`, with the same response class, piece size, 30-second piece timeout and 300-second duration cap; 300 s carries the largest whole-document rendition, 104,857,600 bytes, at about 2.8 Mbit/s.
- **Limits:** section 1.11 (`getCitationContent` and `getCitationDocument` row) and the burst limit (ADR 0148).
- **Idempotency:** a read; each call counts in the download window.

### 3.6 Memory

Every memory operation acts on the caller's own `memory` document (container `memories`, partition key and `id` both the caller's `oid`; `13-conversations-and-memory.md` section 7). Administrators have no access to anyone else's. While the ops-config value `memory.enabled` is false every memory operation keeps working and `MemoryView.enabled` is `false`.

**Common steps of the four writes** (ADR 0077): (1) point-read the document; for `updateMemoryFact` and `deleteMemoryFact` a missing document or fact answers `404 memory_fact_not_found`; (2) `If-Match` parsing and comparison with the document's `_etag` (section 1.9): `428 precondition_required`, then `412 precondition_failed`, and for `updateMemorySettings` and `clearMemory` a missing document with a well-formed `If-Match` is `412`; (3) the operation's own checks; (4) `replace_item` conditioned on the same ETag, where a `412` from Cosmos DB (a concurrent change between read and write) answers `412`; (5) after the commit, the audit row through `docchat_core.audit.write_audit_event_after_commit(event)`, which retries 3 times after 0.2, 0.8 and 2 s and, when every attempt fails, emits `audit_write_failed` and counts `docchat.audit.write_failures` without failing the request, because the change is committed; (6) `200` with the new `MemoryView`.

#### 3.6.1 `getMemory` — `GET /v1/memory`

- **Access:** `user`; the caller's own memory document only.
- **Scoped parameters:** none.
- **Request:** no parameters, no body.
- **Responses:** `200` `MemoryView` (section 4.6).
- **Error codes:** Base, Burst.
- **Rules:** when no memory document exists, one is created empty with `create_item`; a `409` there means a concurrent request created it, so it is read and returned.
- **Limits:** the burst limit. **Idempotency:** a read that creates the document once.

#### 3.6.2 `updateMemorySettings` — `PATCH /v1/memory`

- **Access:** `user`, write-limited; the caller's own document.
- **Scoped parameters:** none.
- **Request:** header `If-Match` (the memory `etag`); body `MemorySettingsRequest` (`extra="forbid"`): `paused` (boolean, required). Request example: `{"paused": true}`.
- **Responses:** `200` `MemoryView` with the new `etag`.
- **Error codes:** Base, Burst, Write, Body, Precondition.
- **Rules:** when `paused` already has the requested value the answer is `200` with the current view, and nothing is written or audited. Otherwise the replace sets `paused`, `pausedAt` (set when pausing, kept when resuming), `userEditedAt` and `updatedAt`. While paused, nothing is extracted and the memory block leaves every prompt; facts stay viewable, editable and deletable.
- **Effects:** audit `memory.paused` or `memory.resumed`.

#### 3.6.3 `updateMemoryFact` — `PUT /v1/memory/facts/{factId}`

- **Access:** `user`, write-limited; the caller's own document.
- **Scoped parameters:** `factId` → `404 memory_fact_not_found`.
- **Request:** header `If-Match`; body `MemoryFactRequest` (`extra="forbid"`): `text` (string, required, at most 800 code points as sent, four times the 200-code-point fact limit, which leaves room for the whitespace that normalisation collapses); `category` (`role`, `team`, `expertise`, `topic` or `answer_style`, required). Request example: `{"text": "Works on supplier contract renewals", "category": "topic"}`.
- **Responses:** `200` `MemoryView`.
- **Error codes:** Base, Burst, Write, Body, Precondition; `404 memory_fact_not_found`. `503 dependency_unavailable` also covers fact screening unavailable after its retry.
- **Order and rules:** (1) common steps 1 and 2; (2) normalise `text` to its stored form (Unicode NFC; each run of whitespace, line breaks included, turned into one space; code points of categories Cc, Cf, Co and Cs removed; trimmed) and run the fact validators against every other fact: the first failing reason answers `422 validation_failed` at `body.text` with `empty`, `too_long` (above 200 code points), `contains_link`, `contains_code`, `contains_instruction`, `contains_secret` or `duplicate_fact` (the patterns are `13-conversations-and-memory.md` section 7); (3) a new token total above `memory.max_tokens` answers `422` at `body.text` with `memory_full`; (4) an unchanged text and category answer `200` with no write, no screening and no audit row; (5) when the normalised text differs from the stored text, one Prompt Shields screening call (body `{"userPrompt": <the normalised text>, "documents": []}`, 5-second timeout, one retry): `attackDetected: true` answers `422` at `body.text` with `contains_instruction`, and screening unavailable after the retry answers `503 dependency_unavailable` with nothing written; a change of category alone isn't screened, because the stored text already was; (6) the replace sets the fact's `text`, `category`, `tokens` (counted with the chat encoding), `source` (`{kind: "user", sessionId: null, turnId: null}`) and `updatedAt`, and the document's `userEditedAt` and `updatedAt`.
- **Effects:** audit `memory.fact_updated`; the metric `docchat.memory.fact_screenings` (`source` `user`, `outcome` `passed`, `flagged` or `unavailable`) for each screening.

#### 3.6.4 `deleteMemoryFact` — `DELETE /v1/memory/facts/{factId}`

- **Access:** `user`, write-limited; the caller's own document.
- **Scoped parameters:** `factId` → `404 memory_fact_not_found`.
- **Request:** header `If-Match`; no body.
- **Responses:** `200` `MemoryView`.
- **Error codes:** Base, Burst, Write, Params, Precondition; `404 memory_fact_not_found`.
- **Rules:** common steps; the replace removes the fact and sets `userEditedAt` and `updatedAt`.
- **Effects:** audit `memory.fact_deleted`.

#### 3.6.5 `clearMemory` — `DELETE /v1/memory/facts`

- **Access:** `user`, write-limited; the caller's own document.
- **Scoped parameters:** none.
- **Request:** header `If-Match`; no body.
- **Responses:** `200` `MemoryView`.
- **Error codes:** Base, Burst, Write, Precondition.
- **Rules:** common steps; with no facts the answer is `200` with no write and no audit row; otherwise the replace sets `facts` to `[]`, `clearedAt`, `userEditedAt` and `updatedAt`. The `paused` flag is unchanged.
- **Effects:** audit `memory.cleared` with `factsRemoved`.

### 3.7 Administrator projects and members

#### 3.7.1 Shared transaction steps

Every administrator write of this section runs one SQL transaction with `SET XACT_ABORT ON`, writes its audit rows in that transaction before the commit (so no change exists without its row, and a failed audit insert fails the change; ADR 0029), and maps SQL failures through the data-access layer (`503 dependency_unavailable` after its retries). `adminUpdateProject` and the four member and mapping operations share two steps (ADR 0022):

- **Version check (V):** `SELECT status, row_version FROM app.projects WITH (UPDLOCK, ROWLOCK) WHERE id = :project_id`: no row → `404 project_not_found`; then the operation's own lookups (`404`); then a missing or malformed `If-Match` → `428 precondition_required`; `row_version` other than the tag → `412 precondition_failed`; then `status = 'archived'` → `409 project_archived` (for `adminUpdateProject`, the archive rules of section 3.7.5 instead).
- **Version move (M), only when the transaction changed a row:** `UPDATE app.projects SET updated_at = SYSUTCDATETIME(), updated_by = :caller OUTPUT inserted.row_version WHERE id = :project_id AND row_version = :rv`. Under the lock it updates exactly one row (zero rows: roll back and re-read; the row gone → `404`, otherwise `412`); its output is the response's `projectRowVersion`. A call that changed no row runs no M and returns `:rv`.

The project-row lock serialises every member and mapping change with archiving, with field edits and with each other, so no change lands on a project archived in between, and the version check refuses every change made from an older view of the project. A membership or mapping change also makes another administrator's pending edit of the project's fields stale, which costs that administrator one reload.

#### 3.7.2 `adminListProjects` — `GET /v1/admin/projects`

- **Access:** `admin`. Archived projects are listed.
- **Scoped parameters:** none.
- **Request:** query `status` (`active`, `archived` or `all`; default `all`), `q` (`SingleLineText(1, 80)`, optional: a case-insensitive prefix of the name, with `\`, `%`, `_` and `[` escaped), `limit` (1 to 100, default 50), `cursor`.
- **Responses:** `200` `AdminProjectSummaryPage` (`items` of `AdminProjectSummary`), ordered by `name`.
- **Error codes:** Base, Burst, Admin, Params.
- **Cost:** one query: the page from `UQ_projects_name` (no status filter) or `IX_projects_status_name`, member and index counts from seeks on `IX_project_members_project` and `PK_project_indexes`, and the default index's display name from the primary key of `app.search_indexes`.
- **Limits:** the burst limit. **Idempotency:** a pure read.

#### 3.7.3 `adminCreateProject` — `POST /v1/admin/projects`

- **Access:** `admin`, write-limited.
- **Scoped parameters:** `body:/initialMemberIds` → `422 validation_failed` with reason `not_found` at `body.initialMemberIds.<i>`.
- **Request:** body `ProjectCreate` (`extra="forbid"`):

| Field | Type | Required | Rules |
|---|---|---|---|
| `name` | `SingleLineText(1, 80)` | yes | unique among all projects, archived included, case-insensitive |
| `description` | `SingleLineText(0, 400)` | no (default `""`) | |
| `instructions` | `MultiLineText(0, 2000)` | no (default `""`) | plain text with LF line breaks; the chat prompt carries it as its own block |
| `exampleQuestions` | array of 0 to 4 `SingleLineText(1, 160)` | no (default `[]`) | two entries equal after case folding: `duplicate` at the later entry's index |
| `isDefault` | boolean | no (default `false`) | a default project is joined by every user at their first sign-in |
| `initialMemberIds` | array of 0 to 50 UUIDs | no (default `[]`) | unique (a repeat: `duplicate` at `body.initialMemberIds.<i>`); each must have an `app.users` row, else `422 validation_failed` with one `{path: "body.initialMemberIds.<i>", reason: "not_found"}` per unknown ID, `<i>` the zero-based index in the request |

Request example: `{"name": "Field notes", "description": "Shared notes for the field team", "instructions": "", "exampleQuestions": [], "isDefault": false, "initialMemberIds": []}`.
- **Responses:** `201` `AdminProject`: a new UUID version 4, `status: active`, `memberCount` the number of initial members, `indexCount` 0, `defaultIndex: null`.
- **Error codes:** Base, Burst, Admin, Write, Body; `409 project_name_taken`.
- **Transaction:** when `initialMemberIds` is non-empty, `SELECT object_id FROM app.users WITH (UPDLOCK, ROWLOCK) WHERE object_id IN (…)` (an expanding bound parameter of at most 50 values; the lock keeps a concurrent user deletion waiting until the commit), and any ID missing answers the `422` above with nothing written; `INSERT app.projects` (`created_by` and `updated_by` the caller); one `INSERT app.project_members` per initial member (`grant_source = N'admin'`, `granted_by` the caller); the audit row `project.created` (`isDefault`), then one `project_member.added` (`grantSource` `admin`) per member; commit. A unique-key violation (2627 or 2601 on `UQ_projects_name`) answers `409 project_name_taken`.
- **Idempotency:** a repeated create with the same name gets `409 project_name_taken`; no `If-Match`. `isDefault: true` affects only users who sign in for the first time afterwards.

#### 3.7.4 `adminGetProject` — `GET /v1/admin/projects/{projectId}`

- **Access:** `admin`. Archived projects are returned.
- **Scoped parameters:** `projectId` → `404 project_not_found`.
- **Request:** path `projectId`.
- **Responses:** `200` `AdminProject`.
- **Error codes:** Base, Burst, Admin, Params; `404 project_not_found`.
- **Limits:** the burst limit. **Idempotency:** a pure read. The administrator screens read it before the member and mapping lists (section 1.9).

#### 3.7.5 `adminUpdateProject` — `PATCH /v1/admin/projects/{projectId}`

- **Access:** `admin`, write-limited.
- **Scoped parameters:** `projectId` → `404 project_not_found`.
- **Request:** header `If-Match` (the project's `rowVersion`); body `ProjectUpdate` (`extra="forbid"`, at least one field, else `422` at `{path: "body", reason: "required"}`): `name`, `description`, `instructions`, `exampleQuestions` and `isDefault`, each with the type and limits of `ProjectCreate`, and `status` (`active` or `archived`). `initialMemberIds` isn't a field here (`unknown_field`); members change through sections 3.7.7 and 3.7.8. Request example: `{"description": "Shared notes for the field team, updated"}`.
- **Responses:** `200` `AdminProject` with the project's new `rowVersion` (or the unchanged one when nothing changed).
- **Error codes:** Base, Burst, Admin, Write, Body, Precondition; `404 project_not_found`; `409 project_name_taken`; `409 project_archived`.
- **Order and rules:** (1) body validation; (2) read the row with `UPDLOCK, ROWLOCK` (`404 project_not_found`); (3) `If-Match` (`428`, then `412`); (4) a body whose `status` differs from the stored status must hold no other field, else `422` at `{path: "body.status", reason: "not_allowed"}`; (5) on an archived project only the body `{"status": "active"}` (restore) is accepted, and anything else answers `409 project_archived`; (6) a body equal to the stored values changes nothing, writes no audit row and returns the unchanged resource and `rowVersion`; (7) `{"status": "archived"}` archives: `status = N'archived'`, `archived_at`, `archived_by` (the caller) and `is_default = 0` in one statement, audit `project.archived` (`wasDefault`); `{"status": "active"}` on an archived project restores it, clearing `archived_at` and `archived_by` with `is_default` staying `0`, audit `project.restored`; any other change runs `UPDATE app.projects SET …, updated_at = SYSUTCDATETIME(), updated_by = :caller OUTPUT inserted.row_version WHERE id = :id AND row_version = :rv` (one row expected) with audit `project.updated` (`changedFields`: the camelCase names among `name`, `description`, `instructions`, `exampleQuestions` and `isDefault` whose values changed); a name collision answers `409 project_name_taken`.
- **Effects:** archiving hides the project's chat, sessions and citations from every user path at once, administrators included; memberships and mappings survive, so a restore brings everything back. No operation deletes a project.

#### 3.7.6 `adminListProjectMembers` — `GET /v1/admin/projects/{projectId}/members`

- **Access:** `admin`. Members of archived projects are listed.
- **Scoped parameters:** `projectId` → `404 project_not_found`.
- **Request:** path `projectId`; query `limit` (1 to 100, default 50), `cursor`.
- **Responses:** `200` `ProjectMemberPage` (`items` of `ProjectMember`), ordered by `displayName`, then `userId`.
- **Error codes:** Base, Burst, Admin, Params; `404 project_not_found`.
- **Cost:** a seek on `IX_project_members_project`, primary-key seeks on `app.users` for members and granters, and a sort of that project's members.

#### 3.7.7 `adminAddProjectMember` — `PUT /v1/admin/projects/{projectId}/members/{userId}`

- **Access:** `admin`, write-limited.
- **Scoped parameters:** `projectId` → `404 project_not_found`; `userId` → `404 user_not_found`.
- **Request:** header `If-Match` (the project's `rowVersion`); no body (a non-empty body answers `422` at `{path: "body", reason: "unknown_field"}`).
- **Responses:** `201` `ProjectMemberChange` when the membership was created, with the project's new `rowVersion`; `200` `ProjectMemberChange` when it already existed (from either source), with the unchanged `rowVersion`. The `member` is read in the same transaction.
- **Error codes:** Base, Burst, Admin, Write, Params, Precondition; `404 project_not_found`; `404 user_not_found`; `409 project_archived`.
- **Order and transaction:** V with the lookup `SELECT 1 FROM app.users WITH (UPDLOCK, ROWLOCK) WHERE object_id = :user_id` (`404 user_not_found`); then `INSERT app.project_members (user_object_id, project_id, grant_source, granted_by) SELECT :user_id, :project_id, N'admin', :caller WHERE NOT EXISTS (SELECT 1 FROM app.project_members WHERE user_object_id = :user_id AND project_id = :project_id)`; one row inserted → audit `project_member.added` (`grantSource` `admin`), M, `201`; none → `200` with `:rv`.
- **Rules:** only people who have signed in at least once have an `app.users` row, and the API has no Microsoft Graph permission, so administrators find and add only those people; a new person signs in, sees the No projects state and asks an administrator (`10-web-client.md` section 4).
- **Idempotency:** a repeat with the returned `projectRowVersion` answers `200` unchanged; a repeat with the version sent before a successful add gets `412`, and the client reloads. The member reaches the project from their next request.

#### 3.7.8 `adminRemoveProjectMember` — `DELETE /v1/admin/projects/{projectId}/members/{userId}`

- **Access:** `admin`, write-limited.
- **Scoped parameters:** `projectId` → `404 project_not_found` (`userId` isn't an existence check: an absent membership answers `removed: false`).
- **Request:** header `If-Match` (the project's `rowVersion`); no body.
- **Responses:** `200` `ProjectPartRemoval`: `removed: true` with the project's new `rowVersion` when a row was deleted; `removed: false` with the unchanged `rowVersion` when none existed.
- **Error codes:** Base, Burst, Admin, Write, Params, Precondition; `404 project_not_found`; `409 project_archived`.
- **Order and transaction:** V (no other lookup); `DELETE FROM app.project_members OUTPUT deleted.grant_source WHERE user_object_id = :user_id AND project_id = :project_id`; one row → audit `project_member.removed` (`grantSource` of the removed row), M, `removed: true`; none → `removed: false` with `:rv`.
- **Effects:** from the person's next request the project is unreachable for them (unless they hold `Admin`) and its sessions are hidden. Their index override for the project, if any, stays and is ignored while the project is unreachable.

### 3.8 Administrator users

#### 3.8.1 `adminSearchUsers` — `GET /v1/admin/users`

- **Access:** `admin`.
- **Scoped parameters:** `projectId` (query) → `404 project_not_found`.
- **Request:** query `q` (`SingleLineText(1, 80)`, optional), `projectId` (UUID, optional), `limit` (1 to 100, default 50), `cursor`.
- **Responses:** `200` `UserSummaryPage` (`items` of `UserSummary`), ordered by `displayName`, then `userId`. With `projectId` each item's `isMember` is `true` or `false`; without it, `null`.
- **Error codes:** Base, Burst, Admin, Params; `404 project_not_found`.
- **Rules:** when `q` is a canonical UUID the match is exact on the user's object ID; otherwise it is a case-insensitive prefix match on the display name or the e-mail address (`LIKE` with `ESCAPE '\'`, the characters `\`, `%`, `_` and `[` in `q` escaped, run as two index seeks combined with `UNION`; query Q6); without `q` every user is listed. Only people who have signed in appear.
- **Cost:** seeks on `IX_users_display_name` and `IX_users_email` (or the primary key for a UUID), and a primary-key seek on `app.project_members` per item when `projectId` is given.

### 3.9 Administrator project-index mappings

A project with at least one mapping has exactly one default mapping, and a project with none has none: the filtered unique index `UX_project_indexes_default` allows at most one, and the two operations below guarantee at least one (ADR 0022).

#### 3.9.1 `adminListProjectIndexes` — `GET /v1/admin/projects/{projectId}/indexes`

- **Access:** `admin`.
- **Scoped parameters:** `projectId` → `404 project_not_found`.
- **Request:** path `projectId`; query `limit` (1 to 100, default 50), `cursor`.
- **Responses:** `200` `ProjectIndexMappingPage` (`items` of `ProjectIndexMapping`), the default mapping first, then by `displayName`, then `indexId`.
- **Error codes:** Base, Burst, Admin, Params; `404 project_not_found`.

#### 3.9.2 `adminMapProjectIndex` — `PUT /v1/admin/projects/{projectId}/indexes/{indexId}`

- **Access:** `admin`, write-limited.
- **Scoped parameters:** `projectId` → `404 project_not_found`; `indexId` → `404 index_not_found`.
- **Request:** header `If-Match` (the project's `rowVersion`); body `ProjectIndexMappingUpdate` (`extra="forbid"`): `isDefault` (boolean, required). Request example: `{"isDefault": true}` (a project's first mapping must be its default).
- **Responses:** `201` `ProjectIndexMappingChange` when the mapping was created; `200` `ProjectIndexMappingChange` when it was updated or unchanged; each with the project's `rowVersion` after the call.
- **Error codes:** Base, Burst, Admin, Write, Body, Precondition; `404 project_not_found`; `404 index_not_found`; `409 project_archived`; `409 index_not_mappable`; `409 default_index_required`.
- **Order and transaction:** V with the lookup of the logical index's `app.search_indexes` row (missing, or `status` `deleted` → `404 index_not_found`, the answer `adminGetIndex` gives too); after V, a status of `failed` or `deleting` → `409 index_not_mappable` (the mappable statuses are `provisioning`, `ready`, `reingest_required`, `reingesting` and `rebuilding`); then read the project's mappings (seek on `PK_project_indexes`) and apply the default rules: a project's first mapping with `isDefault: false` → `409 default_index_required`; `isDefault: false` on the current default → `409 default_index_required`; `isDefault: true` on a mapping that isn't the default (a new one included) first runs `UPDATE app.project_indexes SET is_default = 0, updated_at = SYSUTCDATETIME(), updated_by = :caller WHERE project_id = :project_id AND is_default = 1`, then inserts or updates the target row, so the unique index never sees two defaults. Audit rows: `project_index.mapped` (`isDefault`) when the mapping is created; `project_index.default_set` (`previousDefaultIndexId`) whenever the call makes this mapping the default while another mapping was the default. M when a row was inserted or changed; commit.
- **Idempotency:** an unchanged mapping answers `200` with the unchanged version; a repeat with an older version gets `412`.
- **Effects:** chat resolves the new default from the next request; `listMyProjects` reports `hasDefaultIndex: true` once the default's logical index has an active physical index.

#### 3.9.3 `adminUnmapProjectIndex` — `DELETE /v1/admin/projects/{projectId}/indexes/{indexId}`

- **Access:** `admin`, write-limited.
- **Scoped parameters:** `projectId` → `404 project_not_found` (`indexId` isn't an existence check: an absent mapping answers `removed: false`).
- **Request:** header `If-Match` (the project's `rowVersion`); no body.
- **Responses:** `200` `ProjectPartRemoval`: `removed: true` with the project's new `rowVersion` when the mapping was deleted; `removed: false` with the unchanged `rowVersion` when it didn't exist.
- **Error codes:** Base, Burst, Admin, Write, Params, Precondition; `404 project_not_found`; `409 project_archived`; `409 default_index_in_use`.
- **Order and transaction:** V (no other lookup); read the mapping (none: commit and answer `removed: false` with `:rv`); the default mapping while other mappings exist → `409 default_index_in_use` (removing the only mapping is always allowed); `SELECT COUNT(*) FROM app.user_index_overrides WHERE project_id = :project_id AND search_index_id = :index_id` (seek on `IX_user_index_overrides_mapping`); `DELETE FROM app.project_indexes WHERE project_id = :project_id AND search_index_id = :index_id`, whose overrides the cascading foreign key `FK_user_index_overrides_mapping` deletes; audit `project_index.unmapped` (`wasDefault`, `overridesRemoved`); M; `removed: true`.
- **Effects:** citations to documents of that index from this project's turns answer `404 citation_not_found` from the next request; with no mapping left, chat in the project answers `409 project_index_unavailable`.

### 3.10 Administrator index options and indexes

Administrators act on every index; these routes have no per-project restriction. The index statuses are `provisioning`, `ready`, `reingest_required`, `reingesting`, `rebuilding`, `failed`, `deleting` and `deleted` (section 4.8); a `deleted` index answers `404 index_not_found` on every route. Every operation of sections 3.10 and 3.11 that creates a run commits its transaction first and then sends one queue message `{runId, traceparent, tracestate}` (`80-data-model.md` section 5): a send failure after the queue client's 3 retries is logged as `ingestion_queue_send_failed` (the run ID only) and the request still succeeds, because the ingestion dispatcher's next wake finds the queued items (within `ingestion.idle_wake_minutes`, default 60, when it is idle). The transactions behind these operations are `14-ingestion.md` sections 7 and 8.

#### 3.10.1 `adminGetIndexOptions` — `GET /v1/admin/index-options`

- **Access:** `admin`.
- **Scoped parameters:** none.
- **Request:** no parameters, no body.
- **Responses:** `200` `IndexOptions`:

| Field | Type | Value |
|---|---|---|
| `methods` | array of 1 to 50 `MethodOption` | the active rows of the method catalogue `app.ingestion_methods`, ordered by `key`, then `version` descending |
| `embeddingModels` | array of 1 to 8 `EmbeddingModelOption` | the entries of `DOCCHAT_OPENAI_EMBEDDING_MODELS` |
| `languageAnalyzers` | array of 20 `LanguageAnalyzerOption` | the analyzer allowlist in its order (section 4.8) |
| `defaults` | `IndexDefaults` | `methodKey` `recursive`, `methodVersion` 1, `embeddingModel` `text-embedding-3-large`, `embeddingDimensions` 1024, `languageAnalyzer` `standard.lucene` |
| `uploadLimits` | `UploadLimits` | `maxFileBytes` (the value of `DOCCHAT_UPLOAD_MAX_BYTES`), `maxFilesPerBatch` (the value of `DOCCHAT_UPLOAD_MAX_FILES_PER_BATCH`), `allowedExtensions` (`[".pdf", ".docx", ".pptx"]`) |

`MethodOption`: `key` (string, 1 to 64), `version` (integer, 1 to 1,000), `displayName` (string, 1 to 100), `description` (string, 1 to 500), `parameterSchema` (object: the method version's JSON Schema), `defaultParameters` (object), `supportedContentTypes` (array of 1 to 3 media types). `EmbeddingModelOption`: `model` (string, 1 to 64), `modelVersion` (string, 1 to 32), `dimensions` (array of 1 to 3 integers from 256, 1,024 and 3,072), `defaultDimensions` (integer, one of `dimensions`). Every `parameterSchema` uses only this JSON Schema subset, which the web client renders as a form: a root with exactly `type: object`, `properties`, `required` and `additionalProperties: false`; properties of type `integer` or `number` (with `minimum`, `maximum`, `multipleOf`), `boolean`, `string` with `enum` or `oneOf` of `{const, title}`, `string` with `minLength` and `maxLength`, or `array` of such enumerated strings (with `minItems`, `maxItems`, `uniqueItems: true`); and per property `title`, `description` and `default`. The schemas of the methods `pagewise` version 1 and `recursive` version 1 are `14-ingestion.md` section 9. The default method is `recursive`, because bounded passages of about 512 tokens suit a prompt that keeps 8 sources, while a page-wise passage can reach 8,000 tokens.
- **Error codes:** Base, Burst, Admin.

#### 3.10.2 `adminListIndexes` — `GET /v1/admin/indexes`

- **Access:** `admin`.
- **Scoped parameters:** none.
- **Request:** query `limit` (1 to 100, default 50), `cursor`, `status` (optional, one of the eight statuses; absent means every status except `deleted`), `q` (optional `SingleLineText(1, 80)`: a display-name prefix matched case-insensitively with `LIKE @q + '%' ESCAPE '\'` after escaping `%`, `_`, `[` and `\`).
- **Responses:** `200` `IndexSummaryPage` (`items` of `IndexSummary`), ordered by `displayName`, then `indexId`.
- **Error codes:** Base, Burst, Admin, Params.

#### 3.10.3 `adminCreateIndex` — `POST /v1/admin/indexes`

- **Access:** `admin`, write-limited.
- **Scoped parameters:** none.
- **Request:** body `IndexCreate` (`extra="forbid"`):

| Field | Type | Required | Rules |
|---|---|---|---|
| `displayName` | `SingleLineText(1, 80)` | yes | unique among indexes that aren't `deleted`, case-insensitive |
| `description` | `MultiLineText(0, 1200)` or `null` | no (default `null`) | an empty text is stored as `null` |
| `methodKey` | string, 1 to 64 | yes | a key of the method catalogue |
| `methodVersion` | integer, 1 to 1,000 | no | defaults to the highest active version of the key |
| `methodParameters` | object | no | defaults to the method's default parameters; validated by the method's parameter model |
| `embeddingModel` | string, 1 to 64 | yes | a `model` of the embedding catalogue |
| `embeddingDimensions` | integer, 1 to 3,072 | no | defaults to the model's `defaultDimensions`; must be one of its `dimensions` |
| `languageAnalyzer` | string, 1 to 32 | no (default `standard.lucene`) | a value of the analyzer allowlist |

Request example: `{"displayName": "Policy library", "description": "Shared policy and procedure documents", "methodKey": "recursive", "methodVersion": 1, "methodParameters": {"targetTokens": 512, "overlapPercent": 25, "tableMaxTokens": 1600}, "embeddingModel": "text-embedding-3-large", "embeddingDimensions": 1024, "languageAnalyzer": "standard.lucene"}`.
- **Responses:** `202` `IndexRunStarted` (`index`: `IndexDetail` with `status` `provisioning`; `runId`: the `index_create` run).
- **Error codes:** Base, Burst, Admin, Write, Body; `409 display_name_taken`; `409 search_index_capacity`; `422 method_not_found`, `422 method_inactive`, `422 invalid_method_parameters`, `422 unsupported_embedding_model`, `422 unsupported_dimensions`, `422 unsupported_analyzer`.
- **Order and rules:** (1) body validation (`422 validation_failed`); (2) the method (key and version) exists, else `422 method_not_found` with `[{path: "body.methodKey", reason: "not_found"}]`, and is active, else `422 method_inactive` with `[{path: "body.methodKey", reason: "not_allowed"}]`; (3) the parameters pass the method's parameter model, else `422 invalid_method_parameters` with one entry per failing field at `body.methodParameters.<name>` and the model's reason (`required`, `unknown_field`, `invalid_type`, `out_of_range`, `too_many_items`, `duplicate` or `not_allowed`); (4) the model is in the catalogue, else `422 unsupported_embedding_model` at `body.embeddingModel` with `not_allowed`; the dimensions are offered by the model, else `422 unsupported_dimensions` at `body.embeddingDimensions` with `not_allowed`; the analyzer is in the allowlist, else `422 unsupported_analyzer` at `body.languageAnalyzer` with `not_allowed`; (5) capacity: the physical indexes on the search service that aren't `deleted`, plus 1, must not exceed `DOCCHAT_SEARCH_MAX_INDEXES` − 1, which keeps one slot free for a rebuild, else `409 search_index_capacity`; (6) one transaction inserts the logical index (`provisioning`), its physical index generation 1 (`building`) and an `index_create` run (priority 0) with one `create_physical_index` item, sets the physical index's build run and writes the audit row `index.created`; a unique violation on the display name answers `409 display_name_taken`; (7) after the commit, one queue message.
- **Idempotency:** a repeated create gets `409 display_name_taken`, so no duplicate is created. Uploads to the new index are accepted at once; their items wait until its first physical index exists (`14-ingestion.md` section 8).

#### 3.10.4 `adminGetIndex` — `GET /v1/admin/indexes/{indexId}`

- **Access:** `admin`.
- **Scoped parameters:** `indexId` → `404 index_not_found`.
- **Request:** path `indexId`.
- **Responses:** `200` `IndexDetail`.
- **Error codes:** Base, Burst, Admin, Params; `404 index_not_found`.

#### 3.10.5 `adminUpdateIndex` — `PATCH /v1/admin/indexes/{indexId}`

- **Access:** `admin`, write-limited.
- **Scoped parameters:** `indexId` → `404 index_not_found`.
- **Request:** header `If-Match` (the index's `rowVersion`); body `IndexUpdate` (`extra="forbid"`): every field of `IndexCreate`, each optional, with the same types and limits; `description` also takes `null`, which clears it. At least one field must be present, else `422 empty_update` with `[{path: "body", reason: "required"}]`. Request example: `{"description": "Shared policy and procedure documents, reviewed"}`.
- **Responses:** `200` `IndexDetail`.
- **Error codes:** Base, Burst, Admin, Write, Body, Precondition; `404 index_not_found`; `409 display_name_taken`; `409 index_busy`; `422 empty_update`, `422 method_not_found`, `422 method_inactive`, `422 invalid_method_parameters`, `422 unsupported_embedding_model`, `422 unsupported_dimensions`, `422 unsupported_analyzer`.
- **Order and rules:** (1) body validation and `empty_update`; (2) read the index row with `UPDLOCK, ROWLOCK` (`404 index_not_found` when missing or `deleted`); (3) `If-Match` (`428`, then `412`); (4) the catalogue checks of `adminCreateIndex` steps 2 to 4 against the settings the index will have after the update: a new `methodKey` without `methodParameters` takes the new method's defaults, a new `methodKey` without `methodVersion` takes its highest active version, and `methodParameters` alone is validated against the index's method; (5) the settings fields (method, version, parameters, model, dimensions, analyzer) change only while the status is `ready`, `reingest_required` or `failed`, and `displayName` and `description` in any status except `deleting`, else `409 index_busy`; (6) a body equal to the stored values answers `200` with the current resource, writing nothing; otherwise the guarded `UPDATE … WHERE id = @id AND row_version = @rowVersion` (a unique violation on the display name answers `409 display_name_taken`), then, when a settings field changed and the status isn't `failed`, the status becomes `ready` or `reingest_required` by comparing the active physical index and the served chunk sets with the new settings (`14-ingestion.md` section 8); audit `index.updated` (`changedFields`, the status after the update).
- **Effects:** a change that the active physical index doesn't serve marks the index `reingest_required`, and `reingestRequired: true` shows administrators the re-ingest banner; chat keeps serving from the active physical index until a re-ingest or rebuild completes.

#### 3.10.6 `adminDeleteIndex` — `DELETE /v1/admin/indexes/{indexId}`

- **Access:** `admin`, write-limited.
- **Scoped parameters:** `indexId` → `404 index_not_found`.
- **Request:** header `If-Match` (the index's `rowVersion`); no body.
- **Responses:** `202` `IndexRunStarted` (`index` with `status` `deleting`; `runId`: the `index_delete` run).
- **Error codes:** Base, Burst, Admin, Write, Params, Precondition; `404 index_not_found`; `409 index_mapped`; `409 index_busy`.
- **Order and rules:** (1) read the index row with `UPDLOCK, ROWLOCK` (`404 index_not_found`); (2) `If-Match` (`428`, then `412`); (3) an `app.project_indexes` row references the index → `409 index_mapped` (no override can exist without a mapping); (4) a status other than `ready`, `reingest_required` or `failed` → `409 index_busy`; (5) one transaction (ADR 0052): the guarded status update to `deleting`; every live document deleted as a single document delete is, without per-document runs; every `queued` item of the index `cancelled` with `index_deleted`; an `index_delete` run (priority 1) with one `delete_physical_index` item per physical index not yet `deleted` and one `purge_index_blobs` item; audit `index.deleted` (`runId`, `physicalIndexCount`, `documentCount`); (6) after the commit, one queue message.
- **Effects:** while `deleting`, uploads answer `409 index_not_accepting_uploads`, `adminUpdateIndex`, `adminStartReingest`, `adminDeleteDocument` and `adminReingestDocument` answer `409 index_busy`, and `adminMapProjectIndex` answers `409 index_not_mappable`. When the run completes the index becomes `deleted`: every route then answers `404 index_not_found`, `adminListIndexes` omits it unless asked for `status=deleted`, its display name becomes free, and citations into it answer `404 citation_not_found`.

#### 3.10.7 `adminStartReingest` — `POST /v1/admin/indexes/{indexId}/reingest`

- **Access:** `admin`, write-limited.
- **Scoped parameters:** `indexId` → `404 index_not_found`.
- **Request:** header `If-Match` (the index's `rowVersion`); body `ReingestRequest`, exactly `{}` (section 1.7).
- **Responses:** `202` `IndexReingestStarted`: `index` (`IndexDetail`, status `reingesting` or `rebuilding`), `runId`, `mode` (`same_index` or `new_physical_index`), `itemCount` (integer ≥ 0, the `ingest` items created). A run with no items completes at the dispatcher's next sweep.
- **Error codes:** Base, Burst, Admin, Write, Body, Precondition; `404 index_not_found`; `409 index_busy`; `409 search_index_capacity`.
- **Order and rules:** (1) body validation; (2) read the index row with `UPDLOCK, ROWLOCK` (`404`); (3) `If-Match` (`428`, then `412`); (4) a status other than `ready`, `reingest_required` or `failed` → `409 index_busy`; (5) in one transaction under the logical index row's update lock, the mode: `new_physical_index` when the index has no active physical index, or the active one's embedding model, dimensions, analyzer or schema version differs from the index's settings and the current schema version; otherwise `same_index`. For `new_physical_index` the capacity check of `adminCreateIndex` step 5 applies (`409 search_index_capacity`); a `failed` index's leftover `building` physical index becomes `retiring` and its queued items move to the new one; a new physical index (`building`, the next generation), an `index_rebuild` run (priority 1) with its `create_physical_index` item first and one `ingest` item per live document's current version; status `rebuilding`. For `same_index`, an `index_reingest` run (priority 1) with one `ingest` item per live document's current version into the active physical index, skipping versions that already have a `queued` or `running` item for that target; status `reingesting`. Audit `index.reingest_started` (`runId`, `mode`, `itemCount`); (6) after the commit, one queue message.
- **Effects:** chat keeps serving from the active physical index during either mode; a rebuild repoints to the new physical index only when every live document is served there (`14-ingestion.md` section 8).

### 3.11 Administrator documents and uploads

#### 3.11.1 `adminListDocuments` — `GET /v1/admin/indexes/{indexId}/documents`

- **Access:** `admin`.
- **Scoped parameters:** `indexId` → `404 index_not_found`.
- **Request:** path `indexId`; query `limit` (1 to 100, default 50), `cursor`, `state` (optional: `ingested`, `in_progress` or `failed`, matching `displayState`), `q` (optional name prefix, 1 to 255 code points, normalised as document names are, section 3.11.5, and matched against the normalised name with `LIKE` and the escaping of `adminListIndexes`).
- **Responses:** `200` `DocumentSummaryPage` (`items` of `DocumentSummary`), ordered by the normalised name, then `documentId`. Deleted documents are never listed.
- **Error codes:** Base, Burst, Admin, Params; `404 index_not_found`.
- **Cost:** the latest version comes from an `OUTER APPLY (SELECT TOP (1) … ORDER BY version_number DESC)` on `IX_document_versions_document_state`; uploader and requester names come from a `LEFT JOIN app.users` inside the page query, never from one lookup per row.

#### 3.11.2 `adminGetDocument` — `GET /v1/admin/indexes/{indexId}/documents/{documentId}`

- **Access:** `admin`.
- **Scoped parameters:** `indexId` → `404 index_not_found`; `documentId` → `404 document_not_found` (a document of another index, a deleted document and a random ID alike).
- **Request:** path `indexId`, `documentId`.
- **Responses:** `200` `DocumentDetail`.
- **Error codes:** Base, Burst, Admin, Params; `404 index_not_found`; `404 document_not_found`.

The PDF of a document's current version, for the document panel's "Download PDF" action, is served by `adminGetDocumentPdf` (section 3.11.10).

#### 3.11.3 `adminDeleteDocument` — `DELETE /v1/admin/indexes/{indexId}/documents/{documentId}`

- **Access:** `admin`, write-limited.
- **Scoped parameters:** `indexId` → `404 index_not_found`; `documentId` → `404 document_not_found`.
- **Request:** header `If-Match` (the document's `rowVersion`); no body.
- **Responses:** `202` `DocumentDeleteStarted` (`runId`: the `document_delete` run).
- **Error codes:** Base, Burst, Admin, Write, Params, Precondition; `404 index_not_found`; `404 document_not_found`; `409 index_busy`.
- **Order and rules:** (1) the index (`404 index_not_found`) and the live document of that index, read with `UPDLOCK, ROWLOCK` (`404 document_not_found`); (2) `If-Match` (`428`, then `412`); (3) the index `deleting` → `409 index_busy`; (4) one transaction (`14-ingestion.md` section 7): the guarded `UPDATE app.documents SET is_deleted = 1, deleted_at, deleted_by, current_version_id = NULL WHERE id = @document AND is_deleted = 0 AND row_version = @rowVersion` (zero rows: `404` when the document is already deleted, `412` when it is live with another version); its `pending`, `ingesting` and `ingested` versions become `superseded` and its `uploading` versions `failed` with `document_deleted`; every served chunk-set row of the document is deleted, which hides it from readers at once; its other chunk sets become `retired`; its `queued` items become `cancelled` with `document_deleted`; a `document_delete` run (priority 0) with one `cleanup_document` item; audit `document.deleted`; (5) after the commit, one queue message.
- **Effects:** running items finish and their flip fails its guard, so a deleted document stays deleted; citations to the document answer `410 document_changed`. A later upload of the same name brings the document back under the same ID (section 3.11.7).

#### 3.11.4 `adminReingestDocument` — `POST /v1/admin/indexes/{indexId}/documents/{documentId}/reingest`

- **Access:** `admin`, write-limited.
- **Scoped parameters:** `indexId` → `404 index_not_found`; `documentId` → `404 document_not_found`.
- **Request:** header `If-Match` (the document's `rowVersion`); body `ReingestRequest`, exactly `{}`.
- **Responses:** `202` `DocumentReingestStarted` when an item was queued; `200` `DocumentReingestStarted` when an item for the current version and target was already `queued` or `running`. `DocumentReingestStarted`: `document` (`DocumentSummary` with the document's new `rowVersion`), `runId` (the new run, or the newest open run holding the existing item), `itemCount` (1 for `202`, 0 for `200`).
- **Error codes:** Base, Burst, Admin, Write, Body, Precondition; `404 index_not_found`; `404 document_not_found`; `409 no_current_version`; `409 index_busy`.
- **Order and rules:** (1) body validation; (2) the index and the live document (`404`); (3) `If-Match` (`428`, then `412`); (4) no current version → `409 no_current_version`; (5) the index `deleting` or `failed` → `409 index_busy`; (6) one transaction: the guarded `UPDATE app.documents SET updated_at = SYSUTCDATETIME(), updated_by = @caller WHERE id = @id AND is_deleted = 0 AND row_version = @rowVersion` (which moves the document's `rowVersion`); a `document_reingest` run (priority 0) with one `ingest` item for the current version, targeting the logical index's `building` physical index when one exists (status `rebuilding`; this is how an administrator retries a rebuild item that failed and blocks the repoint), otherwise the active one; when a `queued` or `running` item already exists for that version and target, no run is created; audit `document.reingest_requested` (`runId` `null` when nothing was queued); (7) after a commit that created a run, one queue message.
- **Effects:** a target whose served chunk set already matches the index's method is skipped by ingestion (`already_served`).

#### 3.11.5 Rules shared by the upload operations

The upload flow is preflight, reserve, upload content, then submit (ADR 0042; the statements are `14-ingestion.md` section 2). The browser computes each file's SHA-256 and sends one file at a time; the API never parses a document and never gives the browser a storage URL, account name, key or SAS.

- **Access and limits.** `admin`; the four operations pass the burst limit and skip the write limit (`write_limited=False`), because their own bounds cap them (section 1.11).
- **Gate order.** After the pre-body checks and the body's schema validation: (1) the index (`404 index_not_found` when missing or `deleted`); (2) the malware-scanning gate: while `DOCCHAT_STORAGE_MALWARE_SCANNING_ENABLED` is `true` and `SELECT TOP (1) 1 FROM app.scan_canaries WHERE result = 'clean' AND written_at > DATEADD(hour, -48, SYSUTCDATETIME())` returns no row (one seek on `IX_scan_canaries_written_at`), the answer is `503 malware_scanning_not_ready` with `retryAfterSeconds` 3,600 and nothing is written; with scanning disabled the gate is open (ADR 0039); (3) an index in status `failed` or `deleting` → `409 index_not_accepting_uploads`; uploads to a `provisioning` index are accepted, and their items wait for its physical index; (4) the operation's own checks.
- **File names.** A name is display metadata only; storage paths use generated IDs. After Unicode NFC and removal of leading and trailing whitespace, a name must be 1 to 255 code points and contain no `/`, no `\` and no code point of category Cc, Cf, Co, Cs or Cn (as Python 3.13's `unicodedata` reports them), else it is an invalid name (`invalid_name` in preflight, `422 invalid_file_name` on reserve); it must end in `.pdf`, `.docx` or `.pptx` (case-insensitive), else its type is unsupported (`unsupported_type`). The normalised name, which identifies a document within an index, is the name after NFC, trimming, collapsing every run of whitespace to one space and `str.casefold()`; the name after NFC and trimming is kept as the document's source name for display.
- **Media types** come from the extension: `.pdf` → `application/pdf`; `.docx` → `application/vnd.openxmlformats-officedocument.wordprocessingml.document`; `.pptx` → `application/vnd.openxmlformats-officedocument.presentationml.presentation`.
- **Hashes** are sent as 64 hexadecimal characters in either case and compared in lower case.

#### 3.11.6 `adminPreflightUploads` — `POST /v1/admin/indexes/{indexId}/uploads/preflight`

- **Access:** `admin`; not write-limited.
- **Scoped parameters:** `indexId` → `404 index_not_found`.
- **Request:** body `UploadPreflightRequest` (`extra="forbid"`): `files`, an array of 1 to 100 `UploadPreflightFile` (`name`: string, 1 to 300; `sizeBytes`: integer, 0 to 1,099,511,627,776; `sha256`: string matching `^[0-9a-fA-F]{64}$`). A list longer than `DOCCHAT_UPLOAD_MAX_FILES_PER_BATCH` answers `422 batch_too_large` with `[{path: "body.files", reason: "too_many_items"}]` after the gate order's step 3. Request example: `{"files": [{"name": "travel-policy.pdf", "sizeBytes": 482113, "sha256": "73b42435e5b4f2d1236c0444208788574db695cc1d77f6a9a7899020f3d44f06"}]}`.
- **Responses:** `200` `UploadPreflightResponse`: `results`, one `UploadPreflightResult` per file in request order: `name` (the name after NFC and trimming), `status` (`new`, `unchanged`, `duplicate`, `update` or `rejected`), `reason` (`invalid_name`, `unsupported_type`, `empty_file`, `too_large` or `duplicate_in_batch` when `rejected`, otherwise `null`), `documentId` (UUID or `null`: the live document with the same normalised name, for `unchanged`, `update` and `duplicate` when one exists), `existingDocument` (`{documentId, name}` or `null`: for `duplicate`, the other live document that holds these bytes, with its source name), `currentVersionNumber` (integer or `null`: the same-name document's current version number, for `unchanged`, `update` and `duplicate`).
- **Error codes:** Base, Burst, Admin, Body; `404 index_not_found`; `409 index_not_accepting_uploads`; `422 batch_too_large`; `503 malware_scanning_not_ready`.
- **Rules:** per file, without I/O: the name rules (`invalid_name`, then `unsupported_type`), then the size (0 → `empty_file`; above `DOCCHAT_UPLOAD_MAX_BYTES` → `too_large`), then `duplicate_in_batch` for a file whose normalised name or SHA-256 equals an earlier file's in the same request. Then one read-only SQL query binds the files not rejected (normalised names and hashes as table-valued lists of at most 100) and the statuses follow, in order: `unchanged` when a live document with the same normalised name has a `pending`, `ingesting` or `ingested` version with the same SHA-256; `duplicate` when another live document of the index has such a version with that SHA-256; `update` when a live document with the same normalised name has any `pending`, `ingesting` or `ingested` version (the web client asks for confirmation); otherwise `new` (a deleted document's name and a document whose versions all failed included). A failed query answers `503 dependency_unavailable`.
- **Idempotency:** read-only and advisory; reserve and commit enforce the same rules atomically.

#### 3.11.7 `adminReserveUpload` — `POST /v1/admin/indexes/{indexId}/uploads`

- **Access:** `admin`; not write-limited. The upload goes through a project, which must be an active project mapped to the index.
- **Scoped parameters:** `indexId` → `404 index_not_found`; `body:/projectId` → `422 project_not_mapped` with `[{path: "body.projectId", reason: "not_allowed"}]` (an archived project, a project of another index and a random ID alike).
- **Request:** body `UploadReservationRequest` (`extra="forbid"`): `name` (string, 1 to 300), `sizeBytes` (integer, 0 to 1,099,511,627,776), `sha256` (string, `^[0-9a-fA-F]{64}$`), `projectId` (UUID), `confirmUpdate` (boolean, default `false`). Request example: `{"name": "travel-policy.pdf", "sizeBytes": 482113, "sha256": "73b42435e5b4f2d1236c0444208788574db695cc1d77f6a9a7899020f3d44f06", "projectId": "ef87ff47-57f2-42a1-8c93-4ea2bca4792c", "confirmUpdate": false}`.
- **Responses:** `201` `UploadReservation`: `documentId` (UUID), `versionId` (UUID), `versionNumber` (integer ≥ 1), `reused` (boolean, true when an `uploading` version of the document with the same bytes was reused).
- **Error codes:** Base, Burst, Admin, Body; `404 index_not_found`; `409 index_not_accepting_uploads`; `409 content_unchanged`; `409 duplicate_content`; `409 update_not_confirmed`; `422 invalid_file_name`; `422 unsupported_type`; `422 empty_file`; `422 project_not_mapped`; `503 malware_scanning_not_ready`.
- **Order and rules:** after the gate order: (1) the name: an invalid name → `422 invalid_file_name` with `[{path: "body.name", reason: "invalid_characters" | "too_short" | "too_long"}]`, an unsupported extension → `422 unsupported_type` with `[{path: "body.name", reason: "invalid_format"}]`; (2) the size: 0 → `422 empty_file` with `[{path: "body.sizeBytes", reason: "out_of_range"}]`, above `DOCCHAT_UPLOAD_MAX_BYTES` → `422 validation_failed` with `[{path: "body.sizeBytes", reason: "out_of_range"}]`; (3) `projectId` names an active project mapped to this index (one query on `app.project_indexes` and `app.projects`), else `422 project_not_mapped`; (4) one transaction: the document by index and normalised name with `UPDLOCK`; inserted when absent (a new UUID version 4); a deleted document with that name is brought back with the same ID; its source name follows the request's casing and spacing; when an active (`pending`, `ingesting` or `ingested`) version of the document has this SHA-256 → `409 content_unchanged`; when it has active versions without it and `confirmUpdate` is false → `409 update_not_confirmed`; when another live document of the index has an active version with this SHA-256 → `409 duplicate_content`; an `uploading` version of the document with this SHA-256 is reused (`reused: true`); otherwise a new `uploading` version takes the document's next version number and records the project it came through. A unique violation on the index and normalised name retries the whole transaction once; a second violation, a deadlock after the data layer's retries or another transient SQL error answers `503 dependency_unavailable`, with nothing half-done.
- **Idempotency:** a repeat reuses the `uploading` version; the duplicate checks here are advisory under concurrency, and the filtered unique indexes enforce them at commit (section 3.11.8).

#### 3.11.8 `adminUploadContent` — `PUT /v1/admin/indexes/{indexId}/uploads/{versionId}/content`

- **Access:** `admin`; not write-limited.
- **Scoped parameters:** `indexId` → `404 index_not_found`; `versionId` → `404 version_not_found` (a version of another index and a random ID alike).
- **Route options:** `content_types=('application/octet-stream',)`, `body_limit=None`, `write_limited=False`. The handler reads `request.stream()` with no body model, so the file is never buffered whole; a stream holds at most one 4 MiB block of file data in memory.
- **Request:** path `indexId`, `versionId`; headers `Content-Type: application/octet-stream` and `Content-Length` (required); the body is the file's raw bytes.
- **Responses:** `200` `UploadContentResult` (`version`: `VersionSummary` with `state` `pending`).
- **Error codes:** Base, Burst, Admin, Params; `404 index_not_found`; `404 version_not_found`; `409 index_not_accepting_uploads`; `409 version_not_uploading`; `409 content_unchanged`; `409 duplicate_content`; `411 content_length_required`; `413 file_too_large`; `415 unsupported_media_type`; `422 size_mismatch`; `422 file_signature_mismatch`; `422 hash_mismatch`; `429 upload_busy`; `503 storage_unavailable`; `503 malware_scanning_not_ready`.
- **Before the first byte is read**, in order, after the gate order: (1) the version belongs to the path's index (`404 version_not_found`); (2) its state is `uploading` (`409 version_not_uploading`), except that a `pending` version with the reserved SHA-256 answers `200` with its summary, so a retried request is harmless; (3) `Content-Length` is present (`411 content_length_required`), at most `DOCCHAT_UPLOAD_MAX_BYTES` (`413 file_too_large`) and equal to the reserved size (`422 size_mismatch` with `[{path: "header.Content-Length", reason: "out_of_range"}]`); (4) a free upload stream slot in this worker process, taken without waiting (`429 upload_busy`, `retryAfterSeconds` 5); (5) at the first body read, the media type (`415 unsupported_media_type`).
- **While reading**, in 4 MiB blocks (4,194,304 bytes, the last one shorter): the first block's signature against the reserved type (PDF: `%PDF-` within the first 1,024 bytes; DOCX and PPTX: the bytes `50 4B 03 04` at offset 0, which also refuses encrypted Office files) → `422 file_signature_mismatch` with `[{path: "body", reason: "invalid_format"}]`, nothing staged; each block updates a running SHA-256 and byte count, and a count above `Content-Length` → `422 size_mismatch` with `[{path: "body", reason: "out_of_range"}]`; each block is staged with `stage_block` under a block ID that is the base64 of `<request nonce of 32 hexadecimal characters>-<block index as 8 zero-padded digits>`, so two concurrent streams for one version stage disjoint blocks; staging errors are retried by the Blob client (3 retries, about 1, 3 and 5 seconds) and then answer `503 storage_unavailable`. At the end: a count other than the declared size → `422 size_mismatch`; a SHA-256 other than the reserved one → `422 hash_mismatch` with `[{path: "body", reason: "invalid_format"}]`; neither commits anything.
- **Commit:** `commit_block_list` with the version's media type and `If-None-Match: *`, so a version's path is written once; a `ConditionNotMet` answer means an earlier attempt or a concurrent stream already committed these exact bytes, so the API reads the blob's properties and continues with its `last_modified`. Then the version becomes `pending` with its commit time, the blob's `last_modified`, and `scan_required` and `scan_status` from `DOCCHAT_STORAGE_MALWARE_SCANNING_ENABLED` at that moment; when it is already `pending` under this hash the answer is `200`. A violation of either filtered unique SHA-256 index deletes the blob, marks the version `failed` with `content_unchanged` or `duplicate_content`, and answers `409` with that code. Audit `document.upload_committed`. The stream slot is released in a `finally` block.
- **Disconnects and time:** a client disconnect stops the stream and commits nothing; the version stays `uploading` for a retry, the storage service discards staged blocks after 7 days, and ingestion's sweep deletes versions still `uploading` after `ingestion.stale_upload_hours` (default 24). The whole request must finish within App Service's 230-second front-end limit, which a 50 MB file meets at about 1.8 Mbit/s or faster.

#### 3.11.9 `adminSubmitUploads` — `POST /v1/admin/indexes/{indexId}/uploads/submit`

- **Access:** `admin`; not write-limited.
- **Scoped parameters:** `indexId` → `404 index_not_found`; `body:/versionIds` → `200` with `runId: null` and outcome `not_found` for each such version (a version of another index and a random ID alike).
- **Request:** body `UploadSubmitRequest` (`extra="forbid"`): `versionIds`, an array of 1 to 100 unique UUIDs (a repeat answers `422 validation_failed` with `duplicate` at `body.versionIds.<i>`); a list longer than `DOCCHAT_UPLOAD_MAX_FILES_PER_BATCH` answers `422 batch_too_large` with `[{path: "body.versionIds", reason: "too_many_items"}]` after the gate order's step 3. Request example: `{"versionIds": ["fd19225e-a461-49e2-9a2f-87b737e1f304"]}`.
- **Responses:** `202` `UploadSubmitResult` when at least one item was created; `200` `UploadSubmitResult` with `runId: null` when none was. `UploadSubmitResult`: `runId` (UUID or `null`), `results` (one `{versionId, outcome}` per requested version in request order; `outcome` is `queued`, `not_found` (not a version of this index), `not_pending` (its state isn't `pending`) or `already_queued` (a `queued` or `running` `ingest` item exists for it)).
- **Error codes:** Base, Burst, Admin, Body; `404 index_not_found`; `409 index_not_accepting_uploads`; `422 batch_too_large`; `503 malware_scanning_not_ready`.
- **Rules:** one transaction reads the listed versions of this index with `UPDLOCK`, classifies each, picks the target physical index (the active one, or, when there is none, the `building` one, which becomes active as soon as it is provisioned), creates one `upload` run (priority 0) with one `ingest` item per `queued` version, stores the request's W3C trace context (`traceparent`, `tracestate`) on the run, and writes the audit row `ingestion.run_submitted` (`indexId`, `itemCount`, `notQueuedCount`); with nothing to queue it creates no run and writes no audit row. After the commit, one queue message `{runId, traceparent, tracestate}`, so the upload request and every document's processing share one trace.

#### 3.11.10 `adminGetDocumentPdf` — `GET /v1/admin/indexes/{indexId}/documents/{documentId}/pdf`

- **Access:** `admin`. It serves an administrator the same verified whole-document rendition a reader gets, for every format; no operation serves an uploaded file to anyone (`12-chat-pipeline.md` section 16.3), and the supported way for an operator to retrieve one is `docchat-ops document fetch-source` (`14-ingestion.md` section 19.1), whose just-in-time grant also permits a direct read of the container (`82-security.md` section 1.6).
- **Scoped parameters:** `indexId` → `404 index_not_found`; `documentId` → `404 document_not_found` (a document of another index, a deleted document and a random ID alike).
- **Request:** path `indexId`, `documentId`.
- **Responses:** `200` with the whole-document rendition of the document's current version (`rendition_document_path(index_id, document_id, version)` in the container `renditions`), always the full body, with `Content-Type: application/pdf`, `Content-Disposition` from `content_disposition("<stem>.pdf")` (the document's source name without its extension, cut to 251 code points) and every other header of `getCitationContent`.
- **Error codes:** Base, Burst, Admin, Params; `404 index_not_found`; `404 document_not_found`; `409 document_pdf_unavailable`; `429 rate_limited`; `429 too_many_downloads`; `500 document_pdf_missing`.
- **Order and rules:** (1) path validation; (2) the download request window and (3) the download lease of `getCitationContent`, on the administrator's own `usage` partition and shared with the citation operations; both fail closed with `503 dependency_unavailable`; (4) the index (`404 index_not_found`); (5) one SQL statement reading the live document of that index and its current version's `version_number` and `document_pdf_status`: no live document → `404 document_not_found`; no current version, or a `document_pdf_status` other than `built` → `409 document_pdf_unavailable`, with no blob read; (6) the blob: a storage `404` answers `500 document_pdf_missing` and logs `document_pdf_blob_missing`; (7) the stream, as `getCitationContent`, with the same 30-second piece timeout and 300-second duration cap. The lease is released whenever the response ends.
- **Limits:** section 1.11 (`getCitationContent` and `getCitationDocument` row, which this operation shares) and the burst limit (ADR 0148).
- **Idempotency:** a read; each call counts in the download window.

### 3.12 Administrator ingestion runs

#### 3.12.1 `adminListIngestionRuns` — `GET /v1/admin/ingestion-runs`

- **Access:** `admin`.
- **Scoped parameters:** none (`indexId` is a filter, not an existence check: an unknown index gives an empty page).
- **Request:** query `limit` (1 to 100, default 50), `cursor`, `indexId` (UUID, optional), `state` (optional: `queued`, `running`, `completed`, `completed_with_errors` or `failed`), `kind` (optional, one of the eight run kinds of section 4.8).
- **Responses:** `200` `RunSummaryPage` (`items` of `RunSummary`), ordered by `createdAt` descending, then `runId` descending.
- **Error codes:** Base, Burst, Admin, Params.
- **Cost:** the item counts come from one grouped query over `IX_run_items_run` for the page's runs; `requestedByName` from a `LEFT JOIN app.users` inside the page query.

#### 3.12.2 `adminGetIngestionRun` — `GET /v1/admin/ingestion-runs/{runId}`

- **Access:** `admin`.
- **Scoped parameters:** `runId` → `404 run_not_found`.
- **Request:** path `runId`; query `itemsLimit` (1 to 100, default 100), `itemsCursor`, `itemState` (optional, one of the item states of section 4.8).
- **Responses:** `200` `IngestionRunDetail`: `run` (`RunSummary`), `items` (array of 0 to 100 `RunItem`, in queue order), `nextItemsCursor` (string or `null`).
- **Error codes:** Base, Burst, Admin, Params; `404 run_not_found`.

## 4. Shared schemas

### 4.1 Notation

Every model below is a Pydantic response model whose JSON names are the camelCase aliases shown; every field is always present, `null` where empty. Types: **UUID** is lower-case hyphenated UUID text; **time** is ISO 8601 UTC with milliseconds and `Z`; a string's length is counted in Unicode code points; a **count** is an integer from 0 to 2,147,483,647; **rowVersion** is the 12-character base64 text of section 1.9. The models `Problem` and `FieldError` are section 1.5; `CurrentUser`, `SessionDetail`, `CitationDetail`, `IndexOptions` and the request and result models used by one operation are in that operation's section.

### 4.2 Pages

Each page is a concrete model `{items, nextCursor}`: `items` is an array of 0 to the operation's maximum `limit` entries of the named model, and `nextCursor` is a string of 1 to 512 characters or `null` (section 1.8).

| Page model | Items | Operation |
|---|---|---|
| `ProjectSummaryPage` | `ProjectSummary` | `listMyProjects` |
| `AdminProjectSummaryPage` | `AdminProjectSummary` | `adminListProjects` |
| `ProjectMemberPage` | `ProjectMember` | `adminListProjectMembers` |
| `UserSummaryPage` | `UserSummary` | `adminSearchUsers` |
| `ProjectIndexMappingPage` | `ProjectIndexMapping` | `adminListProjectIndexes` |
| `SessionPage` | `SessionSummary` | `listSessions` |
| `TurnPage` | `TurnView` | `listSessionTurns` |
| `IndexSummaryPage` | `IndexSummary` | `adminListIndexes` |
| `DocumentSummaryPage` | `DocumentSummary` | `adminListDocuments` |
| `RunSummaryPage` | `RunSummary` | `adminListIngestionRuns` |

### 4.3 References

| Model | Fields |
|---|---|
| `UserRef` | `userId` (UUID); `displayName` (string, 0 to 256; `""` when no `app.users` row exists for that ID) |
| `IndexRef` | `indexId` (UUID); `displayName` (string, 1 to 80) |
| `ProjectRef` | `projectId` (UUID); `name` (string, 1 to 80) |
| `DocumentRef` | `documentId` (UUID); `name` (string, 1 to 255: the document's source name) |

### 4.4 People and projects

**`ProjectSummary`** (`listMyProjects`):

| Field | Type | Value |
|---|---|---|
| `projectId` | UUID | |
| `name` | string, 1 to 80 | |
| `description` | string, 0 to 400 | |
| `exampleQuestions` | array of 0 to 4 strings of 1 to 160 | |
| `hasDefaultIndex` | boolean | true when the project's default mapping names a logical index whose active physical index is in state `active`, exactly the condition under which `createChatTurn` resolves the project's default (section 1.4.3); false with no mapping, and while the default index has no active physical index (a `provisioning` index whose first physical index isn't active yet, or a `failed` one) |

**`AdminProjectSummary`** (`adminListProjects`): `projectId` (UUID), `name` (string, 1 to 80), `description` (string, 0 to 400), `isDefault` (boolean), `status` (`active` or `archived`), `memberCount` (count), `indexCount` (count), `defaultIndex` (`IndexRef` or `null`), `updatedAt` (time), `rowVersion`.

**`AdminProject`** (`adminCreateProject`, `adminGetProject`, `adminUpdateProject`):

| Field | Type | Value |
|---|---|---|
| `projectId` | UUID | |
| `name` | string, 1 to 80 | |
| `description` | string, 0 to 400 | |
| `instructions` | string, 0 to 2,000 | LF line breaks |
| `exampleQuestions` | array of 0 to 4 strings of 1 to 160 | |
| `isDefault` | boolean | an archived project is never default |
| `status` | `active` or `archived` | |
| `memberCount` | count | |
| `indexCount` | count | |
| `defaultIndex` | `IndexRef` or `null` | |
| `createdAt` | time | |
| `createdBy` | `UserRef` | |
| `updatedAt` | time | the last administrator change to the project's fields, members or mappings |
| `updatedBy` | `UserRef` | |
| `archivedAt` | time or `null` | |
| `archivedBy` | `UserRef` or `null` | |
| `rowVersion` | rowVersion | moves with every administrator change to the project, its members or its mappings |

**`ProjectMember`**: `userId` (UUID), `displayName` (string, 0 to 256), `email` (string, 0 to 320), `grantSource` (`default` or `admin`), `grantedAt` (time), `grantedBy` (`UserRef`, or `null` for `default`), `lastSeenAt` (time).

**`UserSummary`**: `userId` (UUID), `displayName` (string, 0 to 256), `email` (string, 0 to 320), `lastKnownRole` (`Admin` or `User`; display only), `firstSeenAt` (time), `lastSeenAt` (time), `isMember` (boolean, or `null` when the request names no project).

**`ProjectIndexMapping`**: `indexId` (UUID), `displayName` (string, 1 to 80), `indexStatus` (the logical index's status, section 4.8), `isDefault` (boolean), `mappedAt` (time), `mappedBy` (`UserRef`), `updatedAt` (time, the last change of `isDefault`).

**`ProjectMemberChange`**: `member` (`ProjectMember`), `projectRowVersion` (the project's `rowVersion` after the call). **`ProjectIndexMappingChange`**: `mapping` (`ProjectIndexMapping`), `projectRowVersion`. **`ProjectPartRemoval`**: `removed` (boolean: true when a membership or mapping row was deleted, false when none existed), `projectRowVersion`.

### 4.5 Chat and conversations

**`Citation`** is the one public citation shape, used by the `citations` stream event, `TurnView.citations` and replays alike; it never carries a document, version, chunk-set or index identifier, a chunk key or a storage path.

| Field | Type | Value |
|---|---|---|
| `number` | integer, 1 to 16 | numbered in order of first use in the answer |
| `title` | string, 1 to 300 | |
| `pageNumber` | integer, 1 to 100,000 | the cited page of the version's PDF, for every format (for a DOCX or PPTX, a page of the PDF converted from it) |
| `headingPath` | array of 0 to 8 strings of 1 to 200 | |
| `contentType` | `application/pdf`, `application/vnd.openxmlformats-officedocument.wordprocessingml.document` or `application/vnd.openxmlformats-officedocument.presentationml.presentation` | |
| `openAs` | `pdf_page` | every citation opens as the rendition of its page |

**`TurnUsageSummary`**: `promptTokens`, `completionTokens`, `reasoningTokens`, `cachedTokens`, `totalTokens` (counts, summed over every model call of the turn so far) and `estimated` (boolean, true when any call's usage was estimated because it ended without reported usage). The stored per-call usage, costs and billable counts never leave the server.

**`FeedbackView`**: `rating` (`up` or `down`), `category` (`incorrect`, `incomplete`, `not_relevant`, `other`, or `null`), `comment` (string, 1 to 2,000, or `null`), `updatedAt` (time).

**`TurnView`** (`getSession`, `listSessionTurns`):

| Field | Type | Value |
|---|---|---|
| `turnId` | UUID | |
| `seq` | integer, 1 to 5,000 | contiguous within the session |
| `status` | `streaming`, `completed`, `failed` or `cancelled` | a `streaming` turn past its `deadlineAt` is shown as `failed` |
| `question` | string, 1 to 4,000 | as normalised at creation |
| `answer` | string, 0 to 100,000 | with citation tokens; `""` when nothing was released; the partial text of a `cancelled` or `failed` turn |
| `answerTruncated` | boolean | true when generation stopped at the completion cap |
| `errorCode` | a code of section 2.3, `turn_abandoned`, or `null` | the stored failure code; `turn_abandoned` for a `streaming` turn past its deadline |
| `citations` | array of 0 to 16 `Citation` | |
| `allowedLinks` | array of 0 to 50 `https` URLs of at most 2,048 characters | the only URLs the client renders as links in this answer |
| `followups` | array of 0 to 3 strings of 1 to 200 | |
| `feedback` | `FeedbackView` or `null` | |
| `createdAt` | time | |
| `endedAt` | time or `null` | when the turn's status became terminal |

**`SessionSummary`** (`listSessions`): `sessionId` (UUID), `projectId` (UUID), `title` (string, 1 to 80), `createdAt` (time), `lastTurnAt` (time), `turnCount` (integer, 1 to 5,000).

### 4.6 Memory

**`MemoryView`** (every memory operation):

| Field | Type | Value |
|---|---|---|
| `enabled` | boolean | the ops-config value `memory.enabled` |
| `paused` | boolean | |
| `facts` | array of 0 to 200 `FactView` | ordered by category (`role`, `team`, `expertise`, `topic`, `answer_style`), then `updatedAt` newest first |
| `limits` | `MemoryLimits` | |
| `etag` | string, 1 to 128 | the document's Cosmos DB `_etag`, verbatim, double quotes included; the `If-Match` value of the memory writes |
| `updatedAt` | time | the document's latest write |

**`FactView`**: `factId` (UUID), `text` (string, 1 to 200), `category` (`role`, `team`, `expertise`, `topic` or `answer_style`), `source` (`FactSource`), `createdAt` (time), `updatedAt` (time). **`FactSource`**: `kind` (`extracted` or `user`), `sessionId` (UUID of the session the fact came from, or `null` for `user`). **`MemoryLimits`**: `maxFacts` (integer 1 to 200, `memory.max_facts`), `maxFactChars` (integer, 200), `maxTokens` (integer 100 to 4,000, `memory.max_tokens`), `usedFacts` (integer 0 to 200), `usedTokens` (count).

### 4.7 Indexes, documents and runs

**`MethodSummary`**: `key` (string, 1 to 64), `version` (integer, 1 to 1,000), `displayName` (string, 1 to 100). **`MethodDetail`**: the fields of `MethodSummary` and `parameters` (object: the validated parameters with defaults filled in). **`EmbeddingSummary`**: `model` (string, 1 to 64), `dimensions` (256, 1,024 or 3,072).

**`IndexSummary`** (`adminListIndexes`): `indexId` (UUID), `displayName` (string, 1 to 80), `status` (section 4.8), `reingestRequired` (boolean, true exactly when `status` is `reingest_required`; the web client shows the re-ingest banner), `method` (`MethodSummary`), `embedding` (`EmbeddingSummary`), `languageAnalyzer` (an analyzer value, section 4.8), `liveDocumentCount` (count: documents not deleted that have a current version), `updatedAt` (time), `rowVersion`.

**`IndexDetail`** (`adminCreateIndex`, `adminGetIndex`, `adminUpdateIndex`, `adminDeleteIndex`, `adminStartReingest`): every field of `IndexSummary`, with `method` as `MethodDetail`, and:

| Field | Type | Value |
|---|---|---|
| `description` | string, 1 to 1,200, or `null` | |
| `activePhysicalIndex` | `PhysicalIndexSummary` or `null` | the physical index chat searches |
| `buildingPhysicalIndex` | `PhysicalIndexSummary` or `null` | the physical index being built (first creation or rebuild) |
| `documentCounts` | `{live, ingested, inProgress, failed}` (counts) | |
| `rebuildProgress` | `{servedCurrent, liveDocuments}` (counts) or `null` | set only while `rebuilding`: the live documents served in the building index for their current version, of all live documents; the counts behind the repoint condition |
| `mappedProjects` | array of 0 to 200 `ProjectRef`, sorted by name | the projects that map the index; the web client's "upload through project" list |
| `openRun` | `{runId, kind, state}` or `null` | the newest `queued` or `running` run of this index; the web client polls while it is set |
| `createdAt` | time | |
| `createdBy` | UUID | the object ID of the administrator or operator who created it |
| `updatedBy` | UUID | the object ID of the last administrator or operator who changed it |

**`PhysicalIndexSummary`**: `generation` (integer, 1 to 99,999), `schemaVersion` (integer, 1 to 1,000), `embeddingModel` (string, 1 to 64), `embeddingDimensions` (256, 1,024 or 3,072), `languageAnalyzer` (an analyzer value), `state` (section 4.8), `createdAt` (time), `activatedAt` (time or `null`). The search service's index name is never returned.

**`VersionSummary`**:

| Field | Type | Value |
|---|---|---|
| `versionId` | UUID | |
| `versionNumber` | integer, 1 to 2,147,483,647 | never reused within a document |
| `state` | section 4.8 | |
| `sizeBytes` | integer, 1 to 1,099,511,627,776 | |
| `sha256` | string, 64 lower-case hexadecimal characters | |
| `contentType` | one of the three media types | |
| `scanStatus` | section 4.8 | |
| `failureCode` | string, 1 to 64, or `null` | an ingestion item code (section 2.6) |
| `failureMessage` | string, 1 to 400, or `null` | that code's safe message |
| `uploadedBy` | UUID | the uploading administrator's object ID |
| `uploadedByName` | string, 0 to 256, or `null` | the uploader's display name from `app.users`; `null` when no row exists |
| `uploadedViaProject` | `ProjectRef` | the project the upload came through |
| `reservedAt` | time | |
| `committedAt` | time or `null` | |
| `ingestedAt` | time or `null` | |
| `documentPdfStatus` | `built`, `too_large` or `null` | whether Render built the version's whole-document rendition (`80-data-model.md` section 2.13); `null` until its Render completes. `adminGetDocumentPdf` serves the current version's rendition only when this is `built` |

**`DocumentSummary`** (`adminListDocuments`): `documentId` (UUID), `name` (string, 1 to 255: the source name), `contentType` (the latest version's media type, or `null` when the document has no version), `displayState` (below), `currentVersion` (`VersionSummary` or `null`: the version readers see), `latestVersion` (`VersionSummary` or `null`: the highest version number), `updatedAt` (time), `rowVersion`. `displayState` is evaluated in this order: `in_progress` when a version is `uploading`, `pending` or `ingesting`, or an item for the document is `queued` or `running`; else `failed` when the latest version is `failed`; else `ingested` when the document has a current version; else `no_content`.

**`DocumentDetail`** (`adminGetDocument`): every field of `DocumentSummary`, plus `versions` (array of 0 to 100 `VersionSummary`, newest first) and `recentItems` (array of 0 to 20 `RunItem`, the document's newest items first).

**`RunSummary`**:

| Field | Type | Value |
|---|---|---|
| `runId` | UUID | |
| `kind` | section 4.8 | |
| `state` | section 4.8 | |
| `index` | `IndexRef` | |
| `priority` | `interactive` or `bulk` | `interactive` for uploads, single-document runs, index creation and maintenance; `bulk` for index re-ingest, rebuild and deletion |
| `requestedBy` | UUID or `null` | the administrator's or operator's object ID; `null` for system runs |
| `requestedByName` | string, 0 to 256, or `null` | the requester's display name from `app.users`; `null` for system runs and when no row exists |
| `createdAt` | time | |
| `startedAt` | time or `null` | |
| `completedAt` | time or `null` | |
| `counts` | `{total, queued, running, succeeded, skipped, failed, cancelled}` (counts) | |

**`RunItem`**:

| Field | Type | Value |
|---|---|---|
| `itemId` | UUID | |
| `operation` | section 4.8 | |
| `document` | `DocumentRef` or `null` | `null` for index operations |
| `versionNumber` | integer or `null` | |
| `target` | `{generation, state}` of the physical index, or `null` | |
| `state` | section 4.8 | |
| `stage` | section 4.8, or `null` | the last stage reached |
| `attempts` | integer, 0 to 1,000 | the times the item was claimed |
| `outcomeCode` | string, 1 to 64, or `null` | an ingestion item code for `failed`, `skipped` and `cancelled` (section 2.6) |
| `outcomeMessage` | string, 1 to 400, or `null` | that code's safe message |
| `warnings` | array of 0 to 50 `{code, page}` | `code` an ingestion warning code (string, 1 to 64); `page` an integer ≥ 1 or `null` |
| `usage` | `{embeddingTokens, summaryInputTokens, summaryOutputTokens, docIntelPages}`, each a count or `null` | `null` until the item reports the value |
| `createdAt` | time | |
| `startedAt` | time or `null` | |
| `completedAt` | time or `null` | |

### 4.8 Enumerations

| Enumeration | Values |
|---|---|
| Role | `Admin`, `User` |
| Project status | `active`, `archived` |
| Grant source | `default` (from a default project at first sign-in), `admin` |
| Logical index status | `provisioning`, `ready`, `reingest_required`, `reingesting`, `rebuilding`, `failed`, `deleting`, `deleted`; mappable: `provisioning`, `ready`, `reingest_required`, `reingesting`, `rebuilding` |
| Physical index state | `building`, `active`, `retiring`, `deleted` |
| Document version state | `uploading`, `pending`, `superseded`, `ingesting`, `ingested`, `failed` |
| Scan status | `not_required`, `awaiting`, `clean`, `malicious`, `failed`, `unavailable` |
| Document display state | `ingested`, `in_progress`, `failed`, `no_content` |
| Run kind | `upload`, `document_reingest`, `document_delete`, `index_create`, `index_reingest`, `index_rebuild`, `index_delete`, `maintenance` |
| Run state | `queued`, `running`, `completed`, `completed_with_errors`, `failed` |
| Run priority | `interactive`, `bulk` |
| Item operation | `ingest`, `cleanup_document`, `rerender`, `create_physical_index`, `delete_physical_index`, `purge_index_blobs` |
| Item state | `queued`, `running`, `succeeded`, `skipped`, `failed`, `cancelled` |
| Item stage | `validate`, `extract`, `enrich`, `chunk`, `embed`, `index`, `render`, `flip`, `clean_up`, `purge_document`, `create_index`, `delete_index`, `purge_blobs` |
| Re-ingest mode | `same_index`, `new_physical_index` |
| Preflight status | `new`, `unchanged`, `duplicate`, `update`, `rejected` |
| Preflight rejection reason | `invalid_name`, `unsupported_type`, `empty_file`, `too_large`, `duplicate_in_batch` |
| Submit outcome | `queued`, `not_found`, `not_pending`, `already_queued` |
| Turn status | `streaming`, `completed`, `failed`, `cancelled` |
| Stream status | `search_broadened` |
| Feedback rating | `up`, `down` |
| Feedback category | `incorrect`, `incomplete`, `not_relevant`, `other` |
| Fact category | `role`, `team`, `expertise`, `topic`, `answer_style` |
| Fact source kind | `extracted`, `user` |
| Citation `openAs` | `pdf_page` |
| Version `documentPdfStatus` | `built`, `too_large` |
| Document media type | `application/pdf`, `application/vnd.openxmlformats-officedocument.wordprocessingml.document`, `application/vnd.openxmlformats-officedocument.presentationml.presentation` |

**Language analyzers** (the allowlist, in this order; `value` / `label`): `standard.lucene` / Any language; `en.microsoft` / English; `en.lucene` / English, Lucene; `fr.microsoft` / French; `fr.lucene` / French, Lucene; `de.microsoft` / German; `de.lucene` / German, Lucene; `es.microsoft` / Spanish; `es.lucene` / Spanish, Lucene; `it.microsoft` / Italian; `it.lucene` / Italian, Lucene; `nl.microsoft` / Dutch; `nl.lucene` / Dutch, Lucene; `pt-Pt.microsoft` / Portuguese, Portugal; `pt-Br.microsoft` / Portuguese, Brazil; `ja.microsoft` / Japanese; `ko.microsoft` / Korean; `zh-Hans.microsoft` / Chinese, Simplified; `ar.microsoft` / Arabic; `hi.microsoft` / Hindi. A field's analyzer can't change after its physical index is created, so an analyzer change builds a new physical index (`80-data-model.md` section 6).

## 5. Acceptance criteria

1. **Authorisation matrix.** The authorisation matrix test (`84-testing.md` section 3), generated from the document that `app.openapi()` exports, passes for all 45 operations: every operation carries `x-docchat-access`, `x-docchat-scoped-params` and `x-docchat-error-codes`; the `public` set is exactly `GET /healthz`, which answers an anonymous in-process call `200` with an empty body; every other operation answers `401` with no `Authorization` header, with `Bearer not-a-token`, and with no header and a 33 KiB body (never `413`); every non-public operation answers a token without a role `403 role_not_assigned`; every `admin` operation answers a `User` token `403 admin_required` with real IDs, with random IDs and with an invalid body (never `404`, `412`, `422` or `428`); for every scoped parameter, an identifier from another project, user or index and a random identifier both get the answer section 1.13 and the operation's section state, with bodies byte-identical apart from `correlationId`; and the unmodified request with the right caller answers `2xx`. Proof: the authorisation matrix test in CI.
2. **Contract.** Every mounted route appears in the exported document; `GET /openapi.json`, `/docs` and `/redoc` answer `404 route_not_found`; `getHealth` is the only operation with `security: []`; every documented error status references `Problem`; `createChatTurn`'s `200` is `application/x-ndjson` with `ChatStreamEvent`, a `oneOf` over the eight event models discriminated by `type`, and `x-docchat-stream`; regenerating the document reproduces the committed `apps/api/openapi.json` byte for byte. Proof: the contract tests and the CI job `contract`.
3. **Problem bodies.** Every `DocChatProblem` renders exactly the members of section 1.5, with the optional members omitted when they don't apply; `retryAfterSeconds` 2,678,400 validates against `Problem` and the OpenAPI schema while 0 and 2,678,401 are refused; `Retry-After` equals `retryAfterSeconds` on every problem that has it; `sessionId` and `turnId` appear only on the four turn-creation `409` codes as section 1.5 states, and `retryAfterSeconds` on a `409` only for `turn_in_progress`. Proof: unit tests.
4. **Code and reason catalogues.** Every code raised in `apps/api` and `packages/core` is registered once in `docchat_core.errors.CODES` with the title (at most 40 characters) and `detail` of section 2; `status` is `None` for exactly the 13 stream-only codes and `turn_abandoned`; raising one of those 14 as a problem fails the test; every `422` reason raised is in `REASONS`; every `422` response carries at least one `errors[]` entry. Proof: the catalogue unit tests.
5. **No leaks.** A request carrying a sentinel string in every field of every operation never shows the sentinel in any problem body; a route raising an unmapped exception whose message holds a sentinel answers `500 internal_error`, emits exactly one `request_failed` record with `error.type` equal to the exception's qualified type name and `docchat.error_code` `internal_error`, and no log record, span attribute or span event holds the sentinel. Proof: unit tests.
6. **Order of checks.** A call without a token and with an oversize body gets `401`, not `413`; a `User` token on every administrator operation with an invalid body and a random ID gets `403 admin_required`; for every `If-Match` operation the answers come in the order `404`, then `428`, then `412`, then `409` (for example, a stale `If-Match` on an archived project gets `412`, and on a mapped index's `adminDeleteIndex` gets `412`, not `409 index_mapped`); a burst-refused request touches no store. Proof: unit and integration tests per operation family (projects, members and mappings; indexes; documents; memory).
7. **Validation.** Each Pydantic error type maps to the reason of section 1.7; an unknown body field and an unknown query parameter give `unknown_field`; invalid JSON gives `invalid_json`; upper-case UUIDs are accepted and lower-cased while braces, URN and 32-hex forms give `invalid_uuid` in path, query and body; the text types apply NFC, collapse whitespace, convert CR LF and refuse Cc, Cf, Cs, Co and Cn code points (LF and TAB kept in `MultiLineText`); `adminCreateProject` with U+202E or U+200B in `name` answers `422` with `invalid_characters` at `body.name` and writes nothing. Proof: unit tests.
8. **Route options and sizes.** A unit test pins that exactly `createChatTurn`, `adminPreflightUploads`, `adminReserveUpload`, `adminUploadContent` and `adminSubmitUploads` deviate from the default route options, and that every `POST`, `PUT`, `PATCH` and `DELETE` route except those five is write-limited; a 70,000-byte JSON body gets `413 request_too_large` on a default route and a 40,000-byte body on `createChatTurn`; `adminUploadContent` takes `application/octet-stream` with no JSON limit and answers `application/json` with `415` and the shared text, while every other body route answers `application/octet-stream` with `415`. Proof: unit tests.
9. **Paging.** Cursors round-trip; tampered base64, `v` other than 1, an extra member, a filter mismatch and 513 characters each give `422 invalid_cursor`; keyset pages stay complete and duplicate-free while rows are inserted between page reads, and the last page has `nextCursor: null`, for every list operation of section 1.8. Proof: unit and integration tests.
10. **Optimistic concurrency.** `If-Match` missing, `*`, `W/"…"`, a list or an unquoted value gives `428`; a quoted value that isn't a row version gives `412`; the current value passes; for each of the 14 `If-Match` operations a stale value gives `412` and changes nothing, and a call that changes nothing returns the unchanged version; two concurrent `adminMapProjectIndex` calls with the same `If-Match` that make different indexes default give one success and one `412`, leaving exactly one default; after `adminAddProjectMember` the returned `projectRowVersion` equals the `rowVersion` that `adminGetProject` then returns. Proof: unit and integration tests.
11. **Headers.** `200`, `4xx`, `5xx` and `/healthz` responses carry the four common headers of section 1.10; JSON, problem and NDJSON responses carry `Content-Security-Policy: default-src 'none'; frame-ancestors 'none'`; no response has a `Server`, `ETag` or `Content-Encoding` header; `content_disposition` always gives `attachment`, turns a 260-code-point name ending in `.pdf` into a `filename*` of 200 code points ending in `.pdf`, replaces non-ASCII letters, `"`, `\`, `/`, `;`, `%` and control characters with `_` in the ASCII `filename` (at most 200 characters, extension kept) and cuts a name without `.` to 200. Proof: unit tests.
12. **Request limits.** 100 requests in 1 second (test clock) from one `oid` get at least 50 `429 too_many_requests`, each with `retryAfterSeconds` 1 and `Retry-After: 1`, and the refused ones touch no store; another `oid` is unaffected; `createChatTurn` is never refused by the burst limit; after 1 second idle a refused `oid` gets 10 more; a 50,000-user map evicts the least recently used. Inside one minute the write window allows request 120 and refuses request 121; a failing Cosmos DB gives `503 dependency_unavailable`; five ETag conflicts in a row give `429 write_rate_limited` with `retryAfterSeconds` 1. Against the Cosmos DB emulator, 130 `putTurnFeedback` calls from one user starting at second 0 of a UTC minute and 0.4 s apart give exactly 120 successes and 10 `429 write_rate_limited` with `retryAfterSeconds` from 1 to 60 and the same `Retry-After`, and `wrate:{m}` holds `count` 120 and `ttl` 180. Proof: unit and integration tests.
13. **Authentication.** The validator's cases pass: `alg` `none`, `HS256` and `PS256` fail with `bad_alg` before any key lookup; `exp` 59 s past passes and 61 s past fails; `nbf` likewise; another issuer, an `aud` array, another audience, `ver` `1.0` and another `tid` each fail with their own reason; the test client's `azp` passes only in `dev`, `load` and `local` with its setting present; start-up refuses `prod` with a test client ID and equal client IDs; `scp` without `access_as_user` fails; no `roles`, or none of the two roles, gives `403 role_not_assigned`, and `roles` as a string gives `401 bad_roles`; each required claim missing gives `missing_claim`; a 16,385-character token, two `Authorization` headers and the `Basic` scheme fail, `bearer` in lower case passes; an unknown `kid` causes one fetch, a second within 300 s none, 50 concurrent requests one; a set older than 24 hours with a failed fetch gives `503 authentication_unavailable` with `retryAfterSeconds` 30; no source file outside tests contains `x-ms-client-principal` or `x-ms-token`, and a forged `X-MS-CLIENT-PRINCIPAL-ID` doesn't change the caller. Proof: unit tests.
14. **Deployed authentication and CORS.** In `dev`, platform authentication answers `401` without a token and with another audience's token, `403` for an `azp` that isn't allowed and `401` for an anonymous `/healthz`, while the health check reports every instance healthy; a CORS preflight from the web client's origin without a token answers `200` or `204` with `Access-Control-Allow-Origin` equal to that origin, a real call without a token from it gets `401`, a preflight from another origin gets no `Access-Control-Allow-Origin`, and a real call with a token gets `200`. Proof: the deployed tests.
15. **Authorisation semantics.** `authorize_project_for_chat` over {administrator, member, non-member} × {active, archived, missing} allows only (administrator, active) and (member, active), every refusal raising the same `ProjectNotFound`; after `adminRemoveProjectMember`, that person's next chat request in the project answers `404 project_not_found`; an archived project is refused on every chat path for administrators and members alike and a restore brings it back; `hasDefaultIndex` and `resolve_chat_index` agree in every index state. Proof: unit and integration tests, and the deployed membership test.
16. **Isolation.** Every entry of the cross-project and cross-user isolation suite (`84-testing.md` section 4) passes: chat, retrieval with a shared index, sessions (hidden on revocation and visible after a restore), memory (another user's fact ID answers `404` and only the caller's partition key is used), feedback, citations (another user's turn and a revoked project `404`, a superseded version `410`, an unmapped index `404`), documents, uploads and ingestion status (a non-administrator gets `403 admin_required`; an administrator never reads another index's documents or versions through an index's routes), projects, members, mappings, overrides and administrator routes. Proof: the isolation suite in CI.
17. **Stream protocol.** The encoder writes one compact object per line with no raw newline for `\n`, `\r`, U+2028 and emoji; the stream writer's state machine accepts every order section 3.3.3 allows and `error` at every point, and rejects anything after a terminal event, a `status` after a `delta` and anything before `meta`; a fake clock yields a heartbeat after 15 s of silence; every event written validates against its OpenAPI schema; a replay has the stream shape of section 3.3.4 with `replayed: true` and makes no model call and no write; the response headers hold and no `Content-Encoding` appears locally or through the platform for a request with `Accept-Encoding: gzip, br`; a deployed 200-second stream passes uncut through the platform. Proof: unit, integration and deployed tests (the pipeline's failure cases are in `12-chat-pipeline.md`'s acceptance criteria).
18. **Chat pre-stream answers.** Each problem of section 3.3.1 is returned in the chat order of section 1.3, a lease acquired before a later refusal is released before the answer, the duplicates of each turn status answer as section 1.12 states, and each turn-creation `409` carries the extension members of section 1.5; questions that are empty after normalisation, longer than 4,000 code points or hold a refused control character answer `422` at `body.question` with `empty`, `too_long` or `invalid_characters`. Proof: integration tests.
19. **Citations.** A PDF, a DOCX and a PPTX citation each return `openAs: pdf_page`, and `getCitationContent` serves the single cited page with `application/pdf`, `attachment`, `sandbox`, `no-store`, `nosniff` and the blob's `Content-Length`; `getCitationDocument` serves the whole-document rendition with the same headers and `filename*=UTF-8''<stem>.pdf`, and answers `409 document_pdf_unavailable` with no blob read for a version whose `document_pdf_status` is `too_large`; a superseded version, a deleted document and a chunk set whose `pdf_sha256` differs from the version's PDF hash answer `410 document_changed` on all three operations; an unmapped index, another user's session, a removed membership, a streaming turn and an unknown number answer `404 citation_not_found`; no read under `documents/` happens for any citation of any format. With `ratelimit.max_concurrent_downloads` 2, three concurrent downloads by one user, page and whole-document requests mixed, held open by slow readers give two `200` and one `429 too_many_downloads` with `retryAfterSeconds` 10; the 31st request in a minute gets `429 rate_limited` with `retryAfterSeconds` and `Retry-After`; a client that stops reading is aborted after 30 s ± 2 s with `citation_download_aborted` and its lease deleted; a download still running at 300 s (fake clock) is aborted. Proof: integration tests.
20. **Sessions, feedback and memory.** `listSessions` returns only the caller's sessions in projects reachable now; `getSession` shows a `streaming` turn past its deadline as `failed` with `turn_abandoned` and returns exactly the `TurnView` fields; feedback answers `category_requires_down`, `comments_disabled` and `too_long` at their paths and `409 feedback_not_allowed` on a turn without an answer, the last write wins, and `turn_feedback_saved` never carries the comment; `updateMemoryFact` and `deleteMemoryFact` answer `404 memory_fact_not_found` before checking `If-Match`; each fact validator reason, `memory_full`, a Prompt Shields detection (`contains_instruction`) and screening unavailable (`503`) behave as section 3.6.3 states with nothing written on a refusal; an unchanged value writes nothing; every memory change writes its audit row after the commit, and an audit failure after 3 retries emits `audit_write_failed` while the request still succeeds. Proof: integration tests.
21. **Administrator projects and mappings.** `adminCreateProject` with 3 initial members creates 3 memberships and 3 `project_member.added` rows in its transaction; an unknown member ID answers `422` with `not_found` at its index and writes nothing; a repeated ID gives `duplicate` and 51 IDs `too_many_items`; names are unique case-insensitively, archived projects included; an archived project accepts only the restore body; a first mapping with `isDefault: false` answers `409 default_index_required`; removing the default while others exist answers `409 default_index_in_use`; removing the only mapping succeeds, after which chat answers `409 project_index_unavailable`; a `failed` or `deleting` index answers `409 index_not_mappable` and a `deleted` one `404 index_not_found`; a forced audit-insert failure rolls the change back. Proof: integration tests.
22. **Index and upload operations.** `adminUpdateIndex`, `adminStartReingest`, `adminDeleteIndex`, `adminDeleteDocument` and `adminReingestDocument` answer `428` without `If-Match` and `412` when it is stale; `adminDeleteIndex` refuses a mapped index with `409 index_mapped`; a new physical index that would leave no free slot answers `409 search_index_capacity`; the four upload operations answer `503 malware_scanning_not_ready` with `retryAfterSeconds` 3,600 while scanning is enabled and the newest clean canary is 49 hours old, pass with one 47 hours old, and pass with scanning disabled; a 50 MB upload streams through platform authentication with API memory rising by less than 32 MB; a wrong signature, a size different from the reservation and a different SHA-256 commit nothing; the fifth concurrent upload stream on one worker answers `429 upload_busy` with `retryAfterSeconds` 5. Proof: integration and deployed tests.
23. **Request examples.** Each request example of section 1.13 validates against its model and is accepted by an in-process app holding the seeded test data. Proof: the contract test of request examples.
24. **Stop signal.** With `DocChatServer.loop` set to a running loop and `begin_drain` replaced by a recorder, `handle_exit(SIGTERM)` doesn't call the recorder synchronously, the recorder runs once on the next loop iteration, a second SIGTERM adds no call, SIGINT adds none, and Uvicorn's `should_exit` is true after each; with Gunicorn in a subprocess serving an open NDJSON stream, SIGTERM to the master delivers the drain within 1 s while the stream is still open, and the master exits with code 0 within 10 s once the stream ends. Proof: unit and integration tests.
25. **Cheap authenticated call under load.** `getCurrentUser` answers with p95 at most 200 ms beside 100 concurrent chat streams on one smallest `prod`-sized API instance, and makes no SQL call when the caller's last-seen write is under 15 minutes old apart from the lazy ops-config refresh. Proof: the load test (`84-testing.md` section 7).
26. **Index-backed statements.** Every statement of sections 1.4.4, 3.2 and 3.7 to 3.9, run under `SET SHOWPLAN_XML ON` with 10,000 users, 2,000 projects and 100,000 memberships loaded, uses only index seeks or bounded range scans as `84-testing.md` section 2.11 defines them (the ops-config full read excepted). Proof: the plan integration test.
27. **Administrator PDF.** `adminGetDocumentPdf` serves the current version's whole-document rendition of a PDF, a DOCX and a PPTX document with `application/pdf`, `attachment` with `filename*=UTF-8''<stem>.pdf`, `sandbox`, `no-store` and `nosniff`; answers `409 document_pdf_unavailable` for a document without a current version and for one whose current version is `too_large`, `404 document_not_found` for a deleted document and for a document of another index, and `500 document_pdf_missing` when the blob is absent; counts in the caller's download window and lease; and the blob recorder finds no read under `documents/`. Proof: the administrator document PDF integration test.

