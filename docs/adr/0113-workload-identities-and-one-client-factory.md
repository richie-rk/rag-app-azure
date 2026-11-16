# ADR 0113: Workload identities and one client factory

- **Status:** Accepted
- **Date:** 2026-10-21
- **Decided by:** Architect
- **Specified in:** 83-infrastructure.md, 82-security.md, 01-repository.md, 81-config.md, 84-testing.md
- **Related ADRs:** 0006, 0026, 0045, 0068, 0069, 0098, 0103, 0115, 0116, 0121, 0125, 0130, 0150

## Context

No service in this product authenticates with a key, a password, a connection string or a SAS: every data and AI service has local authentication off, and Azure Policy denies turning it back on (ADR 0116), so the only way in is a Microsoft Entra token for a managed identity. Each workload gets only the roles it needs, at the narrowest scope. Clients are created once per process at startup, shared, and closed at shutdown (ADR 0006), and one module creates them all, so credentials, timeouts and endpoints are set in one place. Two client shapes can't exist at startup: search clients per index, because physical index names are data created and retired while the apps run, and the API's blob client tuned for streamed citation files.

Vendor facts (checked 2026-10-14 unless stated):

- A user-assigned identity exists independently of the app it serves, so role assignments and SQL contained users can be created before the app starts. `ManagedIdentityCredential` authenticates one managed identity, chosen by its client ID; `DefaultAzureCredential` combines a chain of credentials, among them `EnvironmentCredential`, which reads a client secret or certificate from environment variables; async credentials live in `azure.identity.aio`, cache tokens in memory and must be closed (checked 2026-10-21).
- Durable Functions identity-based connections for a user-assigned identity take `AzureWebJobsStorage__credential` = `managedidentity` and `AzureWebJobsStorage__clientId`; Durable Functions "does *not* support `managedIdentityResourceId` when using user-assigned identity" (checked 2026-10-21).
- Application Insights with `DisableLocalAuth=true` accepts only Entra-authenticated telemetry; `APPLICATIONINSIGHTS_AUTHENTICATION_STRING` is `Authorization=AAD;ClientId=<client ID>` for a user-assigned identity, and `APPLICATIONINSIGHTS_CONNECTION_STRING` is still required.
- App Service pulls a container image with `acrUseManagedIdentityCreds: true` and `acrUserManagedIdentityID`, which the `Microsoft.Web/sites` schema documents as the identity's client ID; another Learn page says the resource ID, so a stage-0 spike settles it (ADR 0103 criterion 7).

## Decision

1. **One identity per workload.** User-assigned identities (`Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30`, AVM `avm/res/managed-identity/user-assigned-identity` 0.6.0) in each environment's resource group: `id-docchat-api-<env>` for the API web app only, `id-docchat-ingest-<env>` for the ingestion function app only, and `id-docchat-stub-load` for the `load` model stub only (AcrPull and nothing else). No system-assigned identity exists, and no app identity holds a federated credential (denied by policy, ADR 0116). Each identity's roles are the least-privilege set at the narrowest scope (83-infrastructure.md holds the role table).
2. **No keys, connection strings or SAS.** No client accepts one. App settings hold only endpoints, names and identity client IDs: `DOCCHAT_CORE_IDENTITY_CLIENT_ID` (the workload identity's client ID), the service endpoints, `APPLICATIONINSIGHTS_AUTHENTICATION_STRING` = `Authorization=AAD;ClientId=<client ID>`, and `APPLICATIONINSIGHTS_CONNECTION_STRING`, whose instrumentation key is an identifier, not a credential, once local auth is off. The Functions host connects with `AzureWebJobsStorage__blobServiceUri`, `__queueServiceUri`, `__tableServiceUri`, `__credential` = `managedidentity` and `__clientId`, and the queue trigger with `DOCCHAT_INGESTION_QUEUE__queueServiceUri`, `__credential` and `__clientId`. Image pulls use `acrUseManagedIdentityCreds: true` with `acrUserManagedIdentityID` set to the client ID, or the resource ID if the spike says so.
3. **One credential per process.** `docchat_core.clients` creates one `azure.identity.aio.ManagedIdentityCredential(client_id=DOCCHAT_CORE_IDENTITY_CLIENT_ID)` and shares it with every client of the process, so the process holds one token cache. `azure.identity.aio.AzureCliCredential` is used only when `DOCCHAT_CORE_ENVIRONMENT` is `local`; the emulator branch only when `DOCCHAT_LOCAL_EMULATORS` is `true`, and startup stops with `local_emulators_in_azure` when that flag is set while `WEBSITE_SITE_NAME` or `WEBSITE_INSTANCE_ID` exists. `DefaultAzureCredential` is never used.
4. **One factory.** `docchat_core.clients` creates every Azure client of a process once, from `.aio` packages, at startup, and closes each at shutdown: SQL through the data-access layer's thread pool with an access token per connection for `https://database.windows.net/.default` (ADR 0026); `CosmosClient` (`connection_timeout=5`); `BlobServiceClient` and `QueueClient` (`https://storage.azure.com/.default`); `SearchIndexClient` (ingestion); `openai.AsyncOpenAI` on `<DOCCHAT_OPENAI_ENDPOINT>/openai/v1/` with a bearer-token provider for `https://ai.azure.com/.default` (ADR 0068); `DocumentIntelligenceClient` (API 2024-11-30); the `httpx.AsyncClient` for Prompt Shields with tokens for `https://cognitiveservices.azure.com/.default`; and Azure Monitor export through `configure_azure_monitor(connection_string, credential)`. Every endpoint comes from one app setting, and every client sets explicit timeouts (ADR 0150).
5. **Two shapes that can't exist at startup.**
   - `search_client(physical_index_name)` returns the process's `azure.search.documents.aio.SearchClient` for that physical index: created on first use with the shared credential and `api_version="2026-04-01"`; in the API `connection_timeout=10` and `read_timeout=DOCCHAT_SEARCH_TIMEOUT_SECONDS`, in ingestion `connection_timeout=10` and `read_timeout=120`; at most 16 clients kept in an LRU keyed by physical index name; each cached client counts its in-flight requests, and an evicted client is closed with `await client.close()` once they finish; every cached client is closed at shutdown.
   - The API's data-account `BlobServiceClient` sets `max_single_get_size=1048576` and `max_chunk_get_size=1048576` beside `connection_timeout=10` and `read_timeout=60`, so a citation file streams in 1 MiB reads (ADR 0069).
6. **Enforcement.** The AST check `scripts/ci/check_outbound_clients.py` fails CI when an Azure client or credential is constructed outside `docchat_core.clients`, or a transport timeout is missing.

## Alternatives considered

- **System-assigned identities.** Rejected: they exist only with their app, so role assignments, the SQL contained users (whose SID comes from the client ID, ADR 0125) and the tightened role-assignment conditions (ADR 0115) would break whenever an app is re-created.
- **One identity shared by both apps.** Rejected: the API would gain index management and blob writes to `renditions`, and ingestion would gain Cosmos DB and queue send rights it doesn't need.
- **`DefaultAzureCredential`.** Rejected: it walks a chain of sources, so an environment variable or a second identity on the host could change which principal a process runs as.
- **One credential per client.** Rejected: every client would keep its own token cache and call the managed-identity endpoint separately.
- **Search clients created per request, or one per index at startup.** Rejected: per request churns connection pools on the chat path; at startup is impossible, because physical indexes are created and retired while the apps run (ADR 0045). An unbounded cache would keep a retired index's client open for the life of the process.

## Consequences

- No secret exists for any workload: nothing to rotate, leak or commit. The two keys the product can hold sit outside the apps: the content-share key under fallback 1, read by the platform from Key Vault (ADR 0103), and the Static Web Apps deployment token, read at deploy time (ADR 0121).
- Every Azure call carries the workload's own identity, so `CDBDataPlaneRequests`, `StorageBlobLogs` and the SQL audit name the component that made it (ADR 0108).
- Re-creating a workload identity changes its principal ID, so role assignments, the SQL user (ADR 0125) and the tightened conditions (runbook R23) must follow; the first provision after it fails at the tightened condition by design.
- Tests (84-testing.md): the AST check of item 6; a unit test that `search_client` returns the same client for the same name, evicts the least recently used at the seventeenth name and closes an evicted client only after its in-flight request ends; deployed `dev` tests that each data and AI resource rejects key authentication and that the API and ingestion apps reach their stores with their identities.

## References

- Durable Functions identity-based connections (`__credential`, `__clientId`, no `managedIdentityResourceId`): https://learn.microsoft.com/en-us/azure/durable-task/durable-functions/durable-functions-configure-managed-identity (checked 2026-10-21)
- Azure Identity client library (credential chains, async credentials, token caching): https://learn.microsoft.com/en-us/python/api/overview/azure/identity-readme?view=azure-python (checked 2026-10-21)
- Application Insights Entra authentication: https://learn.microsoft.com/en-us/azure/azure-monitor/app/azure-ad-authentication?tabs=python (checked 2026-10-14)
- Custom containers on App Service (`acrUserManagedIdentityID`): https://learn.microsoft.com/en-us/azure/app-service/configure-custom-container?pivots=container-linux (checked 2026-10-14)
- Functions app settings (`APPLICATIONINSIGHTS_AUTHENTICATION_STRING`): https://learn.microsoft.com/en-us/azure/azure-functions/configure-monitoring (checked 2026-10-14)
