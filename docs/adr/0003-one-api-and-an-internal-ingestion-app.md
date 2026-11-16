# ADR 0003: One API and an internal ingestion app

- **Status:** Accepted
- **Date:** 2026-10-18
- **Decided by:** Product owner and architect
- **Specified in:** 00-overview.md, 01-repository.md, 11-api.md, 14-ingestion.md, 82-security.md, 83-infrastructure.md
- **Related ADRs:** 0002, 0004, 0005, 0006, 0011, 0017, 0037, 0042, 0043, 0098, 0113, 0133, 0134

## Context

The product owner requires a backend-for-frontend that holds every credential through managed identities and Key Vault, and a browser that makes no direct call to any Azure service: the browser bundle is public, and a shared key in it would identify no user and couldn't be revoked for one person. Document ingestion is long-running, CPU-heavy work on untrusted files, and it holds write rights on documents, search indexes and the ingestion tables, so nothing outside the system should be able to start, query, stop or purge it. The trust boundaries follow from both: static files with no secrets; one public API, guarded by platform authentication and in-app token validation; everything else behind private endpoints.

Vendor facts that bound the design: Durable Functions' built-in HTTP management API is authorised by the app's system key, which grants every Durable HTTP API and must never be placed in client-side code; the Python Durable client reaches its host through the extension's loopback listener (`http://127.0.0.1:17071/durabletask/`), so a function app with public network access disabled still starts and signals orchestrations; with VNet-secured storage the app scales only with runtime scale monitoring on; the queue trigger needs Storage Queue Data Reader and Message Processor, and a sender needs only Message Sender.

## Decision

1. **Four parts, three deployables.**

| Part | Code | Runs on | Calls |
|---|---|---|---|
| Web client | `apps/web` (`@docchat/web`) | Static Web Apps, static files only | the API, and Microsoft Entra ID for sign-in |
| API | `apps/api` (`docchat_api`, `docchat-api`) | App Service for Linux, as a container; Gunicorn with `uvicorn_worker.UvicornWorker` | SQL, Cosmos DB, the data storage account (blob, queue), AI Search (query), Azure OpenAI, Content Safety |
| Ingestion | `apps/ingestion` (`docchat_ingestion`, `docchat-ingestion`) | Functions Elastic Premium for Linux, as a container; Durable Functions (ADR 0005) | SQL, the data storage account, its own host storage account, AI Search (write and manage), Azure OpenAI (ingestion deployments), Document Intelligence |
| Shared library | `packages/core` (`docchat_core`, `docchat-core`) | inside both Python apps | nothing on its own |

2. **One API for the browser.** The API serves all 45 operations. `GET /healthz` is the only public route (an empty `200` that reads no data); every other route is under `/v1`, admin routes under `/v1/admin`. Index lifecycle, uploads, re-ingest, document deletion and run status are API operations: they write the SQL ledger and enqueue work, and run status is read from SQL. The web client calls only the API, through `src/api/http.ts`, and Entra ID; its CSP `connect-src` is `'self'`, the API origin and `https://login.microsoftonline.com`.
3. **No credential leaves the server side.** The bundle and `config.json` carry exactly four public values: `tenantId`, `clientId`, `apiBaseUrl` and `apiScope`. Each Python app authenticates to Azure with its own user-assigned identity, `id-docchat-api-<env>` or `id-docchat-ingest-<env>`, through `ManagedIdentityCredential(client_id=DOCCHAT_CORE_IDENTITY_CLIENT_ID)`. Key Vault exists for third-party secrets and holds none in the first version.
4. **Ingestion has no HTTP surface.** No HTTP-triggered function exists. The triggers are `ingestion_queue_starter` (queue `ingestion-requests`, connection prefix `DOCCHAT_INGESTION_QUEUE`), `ingestion_keepalive` (timer `0 */5 * * * *`) and `ingestion_maintenance` (timer `0 40 2 * * *`, 02:40 UTC). The function app `func-docchat-ingest-<env>-<token>` has `publicNetworkAccess: 'Disabled'`, no Easy Auth, no CORS, and FTP and SCM basic publishing off, so Durable's built-in webhooks are unreachable; no code reads or hands out a host or system key.
5. **The hand-off is one queue message.** Queue `ingestion-requests` and poison queue `ingestion-requests-poison` live on the data storage account, declared in Bicep. The message `IngestionQueueMessage` (`extra="forbid"`) carries `runId` (UUID), `traceparent` (matching `^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$`) and `tracestate` (at most 512 characters, omitted when empty), as UTF-8 JSON encoded in base64, with a time to live of 86,400 seconds. It never carries document content, a path, a credential, an index or a method. The API identity holds only Storage Queue Data Message Sender on `ingestion-requests`; the ingestion identity holds Storage Queue Data Reader and Message Processor on it, plus Message Sender and Reader on the poison queue. The API holds no role on the host storage account, the task hub or the function app.
6. **SQL is the source of truth.** The API records the run and one item per version in SQL before it sends the message. The ingestion app acts on a message or a `run_queued` event only after re-reading run, item and version state from SQL; the dispatcher's sweep finds a run whose message was lost at its next wake, within 60 seconds while work is outstanding and 60 minutes when idle.
7. **Parsing stays in ingestion.** The API streams an upload to its blob path and checks type, size and hash while streaming, but never parses a document (ADR 0043).
8. **Shared code lives only in `packages/core`.** Its subpackages are `settings`, `clients`, `sql`, `opsconfig`, `telemetry`, `auth`, `errors`, `models`, `access`, `users`, `audit`, `memory`, `chat`, `cosmos`, `search` and `ingestion`. import-linter contracts run in CI: `docchat_core` imports neither app, `docchat_api` and `docchat_ingestion` are independent of each other, and no production package imports `docchat_testing` or `docchat_model_stub`.

## Alternatives considered

- **The browser calls Azure services itself, with keys or SAS tokens.** Lost: the bundle is public, a shared key identifies no user and can't be revoked for one person, and the API couldn't check type, size and hash while a file streams.
- **A second, browser-facing function app for admin and ingestion operations.** Lost: two public surfaces and two authentication layers to configure and test, and a service hop for calls the API can serve directly.
- **An HTTP starter on the ingestion app (function keys or platform authentication).** Lost: function keys are shared secrets; the standard starter response hands out management URLs carrying the system key; and any HTTP surface needs its own platform authentication, CORS, authorisation tests and public network path.
- **The API starts orchestrations through a Durable client.** Lost: the API identity would need data rights on the ingestion host storage account and its task hub, coupling the API to Durable's storage layout; a queue message keeps the ingestion app private, and the SQL ledger keeps the work safe when a message is lost.
- **Azure Service Bus between the apps.** Lost: another resource with its own private endpoint, DNS zone and roles, for one message per upload batch with a one-day lifetime; a Storage queue on the data account, which already has a `queue` private endpoint, needs none of them.
- **Each app with its own copy of shared code.** Lost: token validation, settings, clients and the SQL layer must behave the same in both apps, and two copies drift.

## Consequences

- The internet reaches one API behind two authentication layers; nothing outside the ingestion app can start, query, terminate or purge an orchestration.
- The API carries every browser-facing load (chat streams, citation downloads, 50 MB uploads), and its sizing and scaling tests cover all of it (ADR 0134).
- Admins see ingestion progress from SQL by polling; ingestion has no push channel to the browser.
- Obligations: the authorisation matrix, whose public set must be exactly `GET /healthz`; deployed tests that the ingestion app's default host name refuses every request and that a message on `ingestion-requests` starts the dispatcher within 2 minutes with public access disabled; the import-linter contracts in CI; the browser test that rendering an answer makes no request to any origin but the app, the API and Entra ID.
- Risks: a run whose message is lost waits up to 60 minutes when the dispatcher is idle; a starter message that fails reappears after about 10 minutes, because neither queue role grants `messages/write`. Both only delay work.

## References

- HTTP APIs in Durable Functions (the system key grants every Durable HTTP API and must not be in client-side code): https://learn.microsoft.com/en-us/azure/durable-task/durable-functions/durable-functions-http-api (checked 2026-10-18)
- Durable extension loopback listener: https://raw.githubusercontent.com/Azure/azure-functions-durable-extension/dev/src/WebJobs.Extensions.DurableTask/LocalHttpListener.cs (checked 2026-10-14)
- Azure Functions networking options (runtime scale monitoring with VNet-secured sources): https://learn.microsoft.com/en-us/azure/azure-functions/functions-networking-options (checked 2026-10-14)
- Identity-based connections for the Functions host and triggers: https://learn.microsoft.com/en-us/azure/azure-functions/manage-connections?pivots=functions-auth-identity&tabs=host (checked 2026-10-14)
- Queue trigger (roles, base64 messages, poison queue): https://learn.microsoft.com/en-us/azure/azure-functions/functions-bindings-storage-queue-trigger?pivots=programming-language-python (checked 2026-10-14)
