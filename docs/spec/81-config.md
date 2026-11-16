# 81 — Configuration

This file is the catalogue of every setting DocChat has, and the one place that lists each of them. It names the layers a value can live in and who changes each layer (section 1); lists every app setting, image variable and local setting of the API, the ingestion app and the model stub with its source, type, default per environment, allowed values, reader and purpose (section 2); lists every ops-config key with its area model, default and range, and the procedure that seeds and changes it (section 3); lists every Bicep parameter, environment-file field, shared parameter, bootstrap value, GitHub variable and azd environment value (section 4); specifies the web client's public configuration (section 5); states the rules that refuse an invalid or unsafe configuration (section 6); and lists the values that are code constants and why they aren't settings (section 7). The behaviour each value controls is specified in the file that owns it: `11-api.md` (API conventions and limits), `12-chat-pipeline.md` (chat, retrieval, quotas and pricing), `13-conversations-and-memory.md` (sessions, summaries, memory and feedback), `14-ingestion.md` (uploads, documents, indexes and runs), `10-web-client.md` (the browser), `80-data-model.md` (the stores), `83-infrastructure.md` (resources, identities and delivery), `85-observability.md` (telemetry and alerts) and `86-operations.md` (operator commands and runbooks). ADR 0008 records the layering, ADR 0028 the ops-config mechanism, ADR 0096 the environment files, ADR 0099 the per-deployment Azure OpenAI deployment types and ADR 0086 the deploy-time web configuration.

## 1. Configuration layers

### 1.1 The layers

Every value lives in exactly one layer. No layer that an app reads holds a secret (section 6.2).

| Layer | Where it lives | Holds | Who changes it | Read by | When a change takes effect |
|---|---|---|---|---|---|
| App settings | the App Service and Functions app settings of each site, composed by `infra/modules/compute/app-settings.bicep` (section 2.1) | endpoints, deployment names, models, versions and encodings, identity client IDs, timeouts, upload and process limits, platform switches | a reviewed pull request to `infra/config/app-settings.json` or `infra/env/<env>.json`, applied by the deployment pipeline; values taken from resource or Microsoft Graph outputs change only when the resource changes; nobody sets one by hand, in the portal or by a script | the API, the ingestion app, the model stub, the Functions host, the App Service platform | when the deploy writes the settings: App Service restarts an app on every configuration change, and each process reads its settings once at start |
| Image environment | `ENV` lines of the three Dockerfiles (section 2.9) | values bound to the image's own contents (model and encoding caches, offline switches, the build commit) | a pull request to the Dockerfile; the `build` job of `deploy.yaml` builds the image | the processes in the image | when a release deploys the new image |
| Key Vault references | an app setting whose value is `@Microsoft.KeyVault(SecretUri=…)` | third-party secrets; none exist in the first version, and only content-share fallback 1 adds one (section 2.8) | the pipeline (Bicep) | the Functions host | at the next restart; the platform also refetches a reference every 24 hours |
| Ops config | rows of SQL `app.ops_config` (section 3) | runtime operational values: ingestion parallelism, rate limits, quotas, retrieval and broadening thresholds, summary and memory caps, switches of built features, prices | an operator with just-in-time membership of `sg-docchat-<env>-config-operators`, only through `docchat-ops config set` or `config reset`; in `load` only, the load workflow's configuration jobs (section 3.3) | the API (one snapshot per request, one per turn on the chat path); the ingestion dispatcher (one snapshot per wake) | API: within 60 s while SQL is reachable; ingestion: at the dispatcher's first wake after its snapshot refreshes, never for an item already running |
| Runtime Bicep parameters | `infra/main.bicepparam`, each read from an environment variable at compile time (section 4.1) | environment name, the running image references, the operator allowlist, the resource-token salt, the release commit, the ops mailboxes, the budget start date | the deployment pipeline and its `preprovision` hook; operators, through GitHub environment variables (allowlist, mailboxes); the load workflow (salt) | Bicep, at provisioning | at the next `full` deploy of the environment; `budgetStartDate` acts only when the budget is created |
| Environment files | `infra/env/dev.json`, `infra/env/prod.json`, `infra/env/load.json`, typed by the sealed Bicep type `environmentConfig` (section 4.2) | subscription, tenant, regions, address space, SKUs and instance bounds, zone switches, Azure OpenAI deployments and embedding models with their deployment types, retention, sampling, alert thresholds, budget, group object IDs | a reviewed pull request; the bootstrap writes the subscription, tenant and group IDs | Bicep and the preflight; some fields are surfaced as app settings | at the next `full` deploy of that environment (`prod` after its approval); `cosmos.maxThroughput` acts only when a container is created |
| Shared parameters | `infra/shared.bicepparam` (section 4.6) | the shared registry's and the CI network's regions, the CI path (`ciPrivateNetworking`), the shared operator allowlist, the security workspace | the bootstrap operator | `infra/shared.bicep`, deployed only by the bootstrap's `shared` step | at the next run of that step |
| Bootstrap configuration | `scripts/bootstrap/bootstrap.config.json` (section 4.7) | tenant, subscriptions, region, GitHub organization and repository, the CI path, operators, approvers, security mailboxes, test-user aliases, the principals the role-assignment conditions allow | the bootstrap operator; `tighten_conditions.py` extends two members | `scripts/bootstrap/bootstrap.py`, never `main.bicep` | at the next run of the bootstrap step that reads the value |
| GitHub variables | repository and environment variables of the GitHub repository (section 4.8) | deploy, pull-request and test identity client IDs, tenant, subscription, region, resource group, allowlists, mailboxes, the test vault's URI, the test users' domain, the CI path | the bootstrap (`configure_github.py`), operators, a repository administrator | the workflows | at the next workflow run; the allowlist and the mailboxes reach Azure at the next `full` deploy |
| azd environment | `.azure/<env>/.env` on the runner or the operator's machine, gitignored (section 4.9) | the outputs of `main.bicep` and the values the deploy job and its hooks set | azd (outputs, on every provision or `azd env refresh`), the deploy job (`azd env set`), the hooks | the web configuration generator, `docchat-ops`, the hooks, the deployed tests | at the next provision or refresh |
| Web `config.json` | `/config.json` of the Static Web App, written per environment at deploy time (section 5.1) | exactly four public values: tenant ID, web client ID, API base URL, API scope | the deployment pipeline, through `apps/web/scripts/write-env-config.mjs` | the browser, once at start-up | at the next web deploy; a browser reads it on its next page load (`cache: 'no-store'`) |
| Browser storage | `localStorage` and `sessionStorage` of the web origin (section 5.3) | two display preferences and the sign-out flag | the user; the client on sign-out | the web client | at once |
| Local settings | the developer's gitignored `.env`, the developer's shell, `apps/web/config.local.json` (section 2.11) | local-emulator mode, the local SQL `sa` password, the Vite proxy target | the developer; `docchat-ops local init`; a CI job for its own run | local processes, `compose.yaml`, the Vite dev server | at process start; refused inside Azure (section 6.3) |
| SQL product tables | `app.projects`, `app.project_members`, `app.project_indexes`, `app.search_indexes` and `app.physical_indexes` (`80-data-model.md` sections 2.3 to 2.5, 2.10 and 2.11) | projects (description, instructions, example questions, default flag), logical and physical indexes with their method parameters, mappings, memberships | admins, through the admin API (`11-api.md`) | the API and the ingestion app | on the next request; index records are cached at most 60 s, memberships, mappings and project status never |
| Per-user index overrides | `app.user_index_overrides` | an index that replaces a project's default index for one user, for live testing, at most 30 days | operators, through `docchat-ops override set` and `override clear` | the API | on the next request |
| Method catalogue | `app.ingestion_methods` | ingestion methods, versions and parameter schemas | the Alembic migration that ships a method version | the API and the ingestion app | when the migration runs |

The last three layers hold product data rather than settings; they appear here so that every place a value can be changed is named once. Their tables and rules are in `80-data-model.md`.

### 1.2 Names

1. **App settings** are `DOCCHAT_<AREA>_<NAME>` in upper snake case. The areas are `CORE`, `AUTH`, `API`, `TELEMETRY`, `BUILD`, `SQL`, `COSMOS`, `STORAGE`, `UPLOAD`, `INGESTION`, `SEARCH`, `OPENAI`, `CONTENTSAFETY`, `DOCINTEL`, `STUB`, `LOCAL` and `DEV`.
2. **Platform-defined names keep their own form:** `APPLICATIONINSIGHTS_*`, `AzureWebJobsStorage__*`, `AzureWebJobsScriptRoot`, `AzureFunctionsJobHost__*`, `FUNCTIONS_*`, `WEBSITE_*`, `WEBSITES_*`, and the library variables `HF_*`, `DO_NOT_TRACK`, `SCARF_NO_ANALYTICS`, `TIKTOKEN_CACHE_DIR`, `UNSTRUCTURED_*`, `OMP_NUM_THREADS`, `PYTHON*` and `OTEL_*`.
3. **No app-setting name contains a period,** because Linux Elastic Premium and Dedicated plans don't support such names; host overrides use `__` as the separator. The Functions queue-trigger connection therefore uses the prefix `DOCCHAT_INGESTION_QUEUE__`.
4. **Ops-config keys** are dotted lower case `<area>.<name>`, at most 64 characters of `a` to `z`, `0` to `9`, `_` and `.`, with at least one character before the first dot and no trailing dot (the check constraint `CK_ops_config_key`, `80-data-model.md` section 2.7). The area is the text before the first dot.
5. **Environment-file fields** are camelCase, nested by resource (`api.sku`, `openAi.deployments.chat.capacity`). Bicep parameters are camelCase.
6. **GitHub variables** are upper snake case; azd outputs are upper snake case.
7. **Web values** are the camelCase members of `config.json` and the browser-storage keys `docchat.<name>`.

### 1.3 Change procedures

1. **An app setting with an owner default, or an environment-file field.** Edit `infra/config/app-settings.json` or `infra/env/<env>.json` in a pull request. The infrastructure gate builds `main.bicep` and `main.bicepparam` for all three environments (a missing, extra or mistyped field in any environment file fails the build, section 4.11), lints, runs PSRule and runs the what-if against `dev`; a reviewer approves. On merge to `main`, `deploy.yaml` deploys `dev` in mode `full`, runs the deployed tests, then deploys `prod` after the `prod` environment's required reviewers approve. Provisioning writes the new settings, App Service restarts the app, and each process validates its settings at start (section 6.1). A change of an Azure OpenAI deployment's model or version follows the runbook "Model version upgrade" (`86-operations.md`); a type or capacity change is this same pull request, checked by the preflight (section 4.3).
2. **An ops-config key.** Follow the runbook "Change an ops-config value" (`86-operations.md`): just-in-time membership, `docchat-ops config show`, `config set` with `--ticket` and `--reason`, a 60-second wait, then revocation (section 3.3).
3. **The operator allowlist or the ops mailboxes.** Follow the runbook "Change the operator allowlist": `gh variable set DOCCHAT_OPERATOR_IP_ALLOWLIST --env <env> --body "<comma-separated addresses>"` (for `dev` also `DOCCHAT_DEV_OPERATOR_IP_ALLOWLIST`, which the pull-request what-if reads), then a `full` deploy by `gh workflow run deploy.yaml -f environment=<env> -f image_commit=<current release commit> -f rollback=false`. The ops mailboxes change the same way through `DOCCHAT_OPS_ALERT_EMAILS`. The shared allowlist is exported as `DOCCHAT_SHARED_OPERATOR_IP_ALLOWLIST` in the bootstrap operator's shell and applied with `uv run --frozen python scripts/bootstrap/bootstrap.py --step shared`.
4. **A bootstrap value.** Edit `scripts/bootstrap/bootstrap.config.json` and run the bootstrap step that reads it (`--step <name>`, section 4.7); every step is idempotent.
5. **A web value.** No one edits `config.json`: the deploy regenerates it from the azd outputs on every web deploy (section 5.2).
6. **An image variable.** A pull request to the Dockerfile; the next release builds and deploys the image.

### 1.4 Rollback and compatibility

A rollback (`deploy.yaml` dispatched with `rollback: true`, mode `images-only`) deploys an earlier release's images without provisioning, so earlier code runs against the current settings. Three rules keep that safe:

1. **Ops-config rows are safe:** the loader ignores keys and areas the running code doesn't declare (section 3.1), and only a contract revision deletes a key.
2. **Adding an app setting is safe:** the earlier release's settings classes read only the names they declare.
3. **Removing or renaming an app setting, or setting one outside the range the previous release accepts, is a contract change.** It ships in a later release than the code that stops needing the old name or range, exactly as a contract migration does (`80-data-model.md` section 2.24).

### 1.5 Versions

Each version was confirmed on its official page on 2026-10-14.

| Component | Version | Used for |
|---|---|---|
| Pydantic | 2.14.1 | the ops-config area models and the settings models |
| pydantic-settings | 2.16.0 | every app-settings class |
| tiktoken | 0.14.0 | the encodings `o200k_base` and `cl100k_base`, named by settings, never derived from model names |
| Bicep CLI | 0.47.16 (CI); at least 0.36 for the typed `envConfigs` variable | `main.bicep`, `main.bicepparam`, `infra/types.bicep` |
| Azure Developer CLI (azd) | at least 1.35.1 (`requiredVersions`) | `main.bicepparam` resolution, the azd environment |
| Azure CLI | at least 2.76.0; 2.91.0 in CI | `az webapp config appsettings list` (the app-settings comparison test, section 2.1), `az staticwebapp secrets list` (the web deploy, section 5.2) |
| Node.js | 24.21.0 | `apps/web/scripts/write-env-config.mjs` |
| `@azure/static-web-apps-cli` | 2.0.10 | the web upload that publishes `config.json` |
| `Microsoft.Web/sites/config` | API 2025-03-01 | `authsettingsV2` (section 4.10) |
| `Microsoft.CognitiveServices/accounts/deployments` and `accounts/raiPolicies` | API 2026-07-01 | the Azure OpenAI deployment objects (section 4.3) |
| Microsoft Graph Bicep types | `microsoftgraph/v1.0:1.0.0` | the app registrations whose `appId` values become app settings |

## 2. App settings, image environment and local settings

### 2.1 How each site's settings are composed

1. **One module writes every app setting.** `infra/modules/compute/app-settings.bicep` composes each site's settings from six sources and nothing else: **out** (a resource output of `main.bicep`), **graph** (an output of the Microsoft Graph extension module `infra/modules/identity/entra-apps.bicep`), **env** (a field of the environment's `environmentConfig`, section 4.2), **map** (a SKU map evaluated in `main.bicep`, section 4.5), **def** (the owner's default in `infra/config/app-settings.json`, section 2.2) and **const** (a constant in the module). The tables below give each setting's source.
2. **The set is exact.** Each site gets exactly the settings this section lists for it and no other: the API site 57 settings (sections 2.3, 2.4 and 2.7), the ingestion function app 43 (sections 2.3, 2.5 and 2.7; 46 under content-share fallback 1, section 2.8), the model stub site in `load` 17 (sections 2.6 and 2.7). The deployed app-settings comparison test, step `app-settings-check` of the `deploy` job of `deploy-environment.yaml` (`83-infrastructure.md` section 10.7 step 10), runs as the environment's deploy identity after every deploy in `dev`, `prod` and `load`: it reads the API's and the ingestion app's settings, and in `load` the model stub's, with `az webapp config appsettings list` (permitted by the identity's Contributor, `Microsoft.Web/sites/config/list/action`) and fails the job with `app_settings_mismatch` when the set of names, or any value this catalogue sets for the environment, differs in either direction; the release stays deployed and an operator investigates.
3. **Read once, typed.** Each process reads its settings once at start through Pydantic Settings classes derived from `docchat_core.settings.DocChatSettings` (prefix `DOCCHAT_`, `extra="forbid"`, frozen), so a value can't change while the process runs. Platform-named settings are read through their own names. An invalid or missing value stops the process before it serves (section 6.1).

| Settings class | Package | Holds |
|---|---|---|
| `CoreSettings` | `docchat_core` | `DOCCHAT_CORE_ENVIRONMENT`, `DOCCHAT_CORE_IDENTITY_CLIENT_ID`, `DOCCHAT_CORE_WORKLOAD_PRINCIPAL_ID` (both apps) |
| `AuthSettings` | `docchat_core` | `DOCCHAT_AUTH_TENANT_ID`, `DOCCHAT_AUTH_API_CLIENT_ID`, `DOCCHAT_AUTH_WEB_CLIENT_ID`, `DOCCHAT_AUTH_TEST_CLIENT_ID` (API) |
| `TelemetrySettings` | `docchat_core` | `DOCCHAT_TELEMETRY_SAMPLING_RATIO`, `DOCCHAT_TELEMETRY_LOG_LEVEL`, `DOCCHAT_TELEMETRY_LOOP_LAG_INTERVAL_MS`, `DOCCHAT_BUILD_COMMIT`, `APPLICATIONINSIGHTS_CONNECTION_STRING` (both apps) |
| `SqlSettings` | `docchat_core` | the seven `DOCCHAT_SQL_*` settings (both apps and the migrations) |
| `LocalSettings` | `docchat_core` | `DOCCHAT_LOCAL_EMULATORS`, `DOCCHAT_LOCAL_SQL_SA_PASSWORD` (local runs and tests) |
| `UploadSettings` | `docchat_core.ingestion.settings` | `DOCCHAT_UPLOAD_MAX_BYTES`, `DOCCHAT_UPLOAD_MAX_FILES_PER_BATCH`, `DOCCHAT_UPLOAD_CONCURRENT_STREAMS_PER_WORKER`, `DOCCHAT_SEARCH_MAX_INDEXES`, `DOCCHAT_STORAGE_BLOB_ENDPOINT`, `DOCCHAT_STORAGE_QUEUE_ENDPOINT`, `DOCCHAT_STORAGE_MALWARE_SCANNING_ENABLED` (API) |
| `IngestionAppSettings` | `docchat_core.ingestion.settings` | `DOCCHAT_STORAGE_BLOB_ENDPOINT`, `DOCCHAT_STORAGE_QUEUE_ENDPOINT`, `DOCCHAT_STORAGE_MALWARE_SCANNING_ENABLED`, `DOCCHAT_SEARCH_ENDPOINT`, `DOCCHAT_OPENAI_ENDPOINT`, `DOCCHAT_OPENAI_TABLE_SUMMARY_DEPLOYMENT`, `DOCCHAT_OPENAI_TABLE_SUMMARY_ENCODING`, `DOCCHAT_DOCINTEL_ENDPOINT`, the three `DOCCHAT_INGESTION_PARSE_*` settings (ingestion app) |
| `EmbeddingModelCatalog` | `docchat_core.ingestion.settings` | `DOCCHAT_OPENAI_EMBEDDING_MODELS` (both apps) |
| `ApiSettings` | `docchat_api` | every other `DOCCHAT_*` setting the API reads: `DOCCHAT_COSMOS_*`, `DOCCHAT_SEARCH_ENDPOINT`, `DOCCHAT_SEARCH_TIMEOUT_SECONDS`, `DOCCHAT_OPENAI_ENDPOINT`, the chat and chat-small `DOCCHAT_OPENAI_*` settings, `DOCCHAT_OPENAI_CONNECT_TIMEOUT_SECONDS`, `DOCCHAT_OPENAI_READ_TIMEOUT_SECONDS`, `DOCCHAT_CONTENTSAFETY_*` and the three `DOCCHAT_API_*` process settings |
| `StubSettings` | `docchat_model_stub` | the thirteen `DOCCHAT_STUB_*` settings (model stub) |

`apps/api/gunicorn.conf.py` reads `DOCCHAT_API_WORKERS`, `DOCCHAT_API_GRACEFUL_TIMEOUT_SECONDS` and `DOCCHAT_API_WORKER_TIMEOUT_SECONDS` through `ApiSettings` before any worker starts, so a value outside its range stops the master before it forks. The Functions host and runtime read the `AzureWebJobsStorage__*`, `AzureFunctionsJobHost__*`, `FUNCTIONS_*` and `DOCCHAT_INGESTION_QUEUE__*` settings; App Service reads the `WEBSITE_*` and `WEBSITES_*` settings. `docchat_core.clients` takes every endpoint from these classes, never from request or stored data (section 6.4).

4. **Who changes a value.** In the tables, "pipeline" means a resource output, Graph output, SKU map or constant: it changes only when the resource, the SKU field or the module changes, through a reviewed pull request. "Pull request" means a reviewed change to `infra/config/app-settings.json` or `infra/env/<env>.json`. Either way the deploy writes the value and the app restarts (section 1.3).
5. **No app setting is a secret** in the first version (section 6.2). The only secret-bearing setting in the design is the content-share connection string of fallback 1, absent in the first version and a Key Vault reference when present (section 2.8).

### 2.2 `infra/config/app-settings.json`

The owners' defaults of every **def** setting, one object per site. Values are strings, because app settings are strings; each settings class parses its own. `main.bicep` loads the file with `loadJsonContent('config/app-settings.json')`; the key `modelStub` is used only while `modelStub.enabled` is `true` (`load`).

```json
{
  "api": {
    "DOCCHAT_TELEMETRY_LOG_LEVEL": "INFO",
    "DOCCHAT_TELEMETRY_LOOP_LAG_INTERVAL_MS": "500",
    "DOCCHAT_SQL_THREADS": "8",
    "DOCCHAT_SQL_POOL_TIMEOUT_SECONDS": "10",
    "DOCCHAT_SQL_CONNECT_TIMEOUT_SECONDS": "30",
    "DOCCHAT_SQL_COMMAND_TIMEOUT_SECONDS": "30",
    "DOCCHAT_SQL_IDLE_RELEASE_MINUTES": "10",
    "DOCCHAT_API_GRACEFUL_TIMEOUT_SECONDS": "110",
    "DOCCHAT_API_WORKER_TIMEOUT_SECONDS": "120",
    "DOCCHAT_UPLOAD_MAX_BYTES": "52428800",
    "DOCCHAT_UPLOAD_MAX_FILES_PER_BATCH": "100",
    "DOCCHAT_UPLOAD_CONCURRENT_STREAMS_PER_WORKER": "4",
    "DOCCHAT_SEARCH_TIMEOUT_SECONDS": "10",
    "DOCCHAT_OPENAI_CHAT_REASONING_EFFORT": "low",
    "DOCCHAT_OPENAI_SMALL_REASONING_EFFORT": "none",
    "DOCCHAT_OPENAI_CHAT_PROMPT_CACHE": "disabled",
    "DOCCHAT_OPENAI_SMALL_PROMPT_CACHE": "disabled",
    "DOCCHAT_OPENAI_CONNECT_TIMEOUT_SECONDS": "10",
    "DOCCHAT_OPENAI_READ_TIMEOUT_SECONDS": "60",
    "DOCCHAT_CONTENTSAFETY_TIMEOUT_SECONDS": "5"
  },
  "ingestion": {
    "DOCCHAT_TELEMETRY_LOG_LEVEL": "INFO",
    "DOCCHAT_TELEMETRY_LOOP_LAG_INTERVAL_MS": "500",
    "DOCCHAT_SQL_THREADS": "4",
    "DOCCHAT_SQL_POOL_TIMEOUT_SECONDS": "10",
    "DOCCHAT_SQL_CONNECT_TIMEOUT_SECONDS": "30",
    "DOCCHAT_SQL_COMMAND_TIMEOUT_SECONDS": "120",
    "DOCCHAT_SQL_IDLE_RELEASE_MINUTES": "10",
    "DOCCHAT_INGESTION_PARSE_MEMORY_LIMIT_MB": "2048",
    "DOCCHAT_INGESTION_PARSE_TIMEOUT_SECONDS": "1200",
    "DOCCHAT_INGESTION_PARSE_SLOTS_PER_PROCESS": "1"
  },
  "modelStub": {
    "DOCCHAT_STUB_WORKERS": "2",
    "DOCCHAT_STUB_RECORD": "false",
    "DOCCHAT_STUB_FIRST_TOKEN_DELAY_MS": "800",
    "DOCCHAT_STUB_TOKEN_DELAY_MS": "25",
    "DOCCHAT_STUB_TOKENS_PER_CHUNK": "2",
    "DOCCHAT_STUB_ANSWER_TOKENS": "400",
    "DOCCHAT_STUB_TEXT_TOKENS": "120",
    "DOCCHAT_STUB_JSON_DELAY_MS": "300",
    "DOCCHAT_STUB_EMBEDDING_DELAY_MS": "40",
    "DOCCHAT_STUB_THROTTLE_EVERY_N": "20",
    "DOCCHAT_STUB_THROTTLE_DEPLOYMENTS": "embed-ingest-te3-large-v1,ingest-summary",
    "DOCCHAT_STUB_RETRY_AFTER_MS": "500",
    "DOCCHAT_STUB_SOURCE_ID_PATTERN": "\\[\\[BEGIN SOURCE (S\\d{1,2}) "
  }
}
```

The file holds the same defaults for every environment; a value that must differ per environment is an environment-file field (section 4.2). A unit test of `infra/` parses the file, checks that each key's member set is exactly the **def** settings that sections 2.3 to 2.6 give that site, and validates each value against its settings class.

### 2.3 Settings on both the API and the ingestion app

None is secret. A change takes effect when the deploy restarts the apps.

| Name | Source | Type | Default (`dev` / `prod` / `load`) | Allowed | Changed by | Read by | Purpose |
|---|---|---|---|---|---|---|---|
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | out: the Application Insights component's `ConnectionString` | string | the environment's component; absent in local runs, where nothing is exported | the component's connection string: an ingestion endpoint and an instrumentation key, which is an identifier and not a credential once local authentication is off on the component | pipeline | `configure_telemetry` in both apps | export target of the Azure Monitor OpenTelemetry distro |
| `APPLICATIONINSIGHTS_AUTHENTICATION_STRING` | const | string | `Authorization=AAD;ClientId=<the app's workload identity client ID>` | that form | pipeline | the Azure Monitor exporter in both apps, which builds its Entra credential from it | Entra-authenticated export, with no credential passed by first-party code |
| `DOCCHAT_TELEMETRY_SAMPLING_RATIO` | env: `telemetrySamplingRatio` | float | 1.0 / 1.0 / 1.0 | 0.1 to 1.0 | pull request | `configure_telemetry` | trace sampling ratio, decided from the trace ID so a trace is kept or dropped whole across both apps; the only sampling setting (section 2.10) |
| `DOCCHAT_TELEMETRY_LOG_LEVEL` | def | enum | `INFO` | `INFO`, `WARNING`, `ERROR`; `DEBUG` only while `DOCCHAT_LOCAL_EMULATORS` is `true` | pull request | `configure_telemetry` | level of the `docchat` logger |
| `DOCCHAT_TELEMETRY_LOOP_LAG_INTERVAL_MS` | def | integer, ms | 500 | 100 to 5,000 | pull request | the event-loop lag monitor | sampling interval of the loop-lag measurement |
| `DOCCHAT_CORE_ENVIRONMENT` | env: `environmentName` | enum | `dev` / `prod` / `load` | `dev`, `prod`, `load`; `local` only on a developer machine (section 6.3) | pipeline | both apps | selects the non-production identity rules (the test client is allowed only in `dev`, `load` and `local`); selects the credential in `docchat_core.clients` (`AzureCliCredential` only in `local`); telemetry dimension |
| `DOCCHAT_CORE_IDENTITY_CLIENT_ID` | out: the workload identity's `clientId` | UUID | the app's own user-assigned identity; required unless `DOCCHAT_CORE_ENVIRONMENT` is `local` | lower-case UUID of the app's own identity | pipeline | `docchat_core.clients` only | client ID of the one `ManagedIdentityCredential` per process |
| `DOCCHAT_CORE_WORKLOAD_PRINCIPAL_ID` | out: the workload identity's `principalId` | UUID | the app's own identity | the app's own identity | pipeline | both apps | `actor_object_id` of `system` audit rows |
| `DOCCHAT_SQL_SERVER` | out: `<server>.database.windows.net` | string | the environment's server | that form | pipeline | both apps, the migrations; `docchat-ops` reads the azd value | SQL host of the data-access layer |
| `DOCCHAT_SQL_DATABASE` | const | string | `docchat` | `docchat` | pipeline | both apps, the migrations, `docchat-ops` | database name |
| `DOCCHAT_SQL_THREADS` | def | integer | 8 on the API, 4 on the ingestion app | 2 to 32 | pull request | the data-access layer | size of the thread pool dedicated to SQL, equal to the connection pool size |
| `DOCCHAT_SQL_POOL_TIMEOUT_SECONDS` | def | integer, s | 10 | 1 to 60 | pull request | the data-access layer | wait for a pooled connection |
| `DOCCHAT_SQL_CONNECT_TIMEOUT_SECONDS` | def | integer, s | 30 | 5 to 60 | pull request | the data-access layer | ODBC login timeout |
| `DOCCHAT_SQL_COMMAND_TIMEOUT_SECONDS` | def | integer, s | 30 on the API, 120 on the ingestion app | 5 to 300 | pull request | the data-access layer | per-statement timeout |
| `DOCCHAT_SQL_IDLE_RELEASE_MINUTES` | def | integer, min | 10 | 1 to 60 | pull request | the data-access layer | releases idle connections, so the `dev` database can auto-pause |
| `DOCCHAT_STORAGE_BLOB_ENDPOINT` | out: the data account's `primaryEndpoints.blob` | URL | the environment's data account | `https://<account>.blob.core.windows.net/` | pipeline | both apps (`UploadSettings`, `IngestionAppSettings`) | blob endpoint of the containers `documents`, `renditions` and `ingestion-work` |
| `DOCCHAT_STORAGE_QUEUE_ENDPOINT` | out: the data account's `primaryEndpoints.queue` | URL | the environment's data account | `https://<account>.queue.core.windows.net/` | pipeline | the API (sends `IngestionQueueMessage` to `ingestion-requests`); the ingestion app (the sweep's 32-message peek of `ingestion-requests-poison` for the poison gauge) | queue endpoint |
| `DOCCHAT_STORAGE_MALWARE_SCANNING_ENABLED` | env: `storage.malwareScanning` | bool | `false` / `true` / `false`; `dev` is `true` only while the deployed malware-scanning test runs (section 4.2) | `true`, `false` | pull request | the API (reservation's scan fields and upload gate), the ingestion app (scan wait, sweep, scanner canaries) | gates ingestion on Defender for Storage scan results (`14-ingestion.md` section 5.4) |
| `DOCCHAT_SEARCH_ENDPOINT` | out | URL | the environment's search service | `https://<service>.search.windows.net` | pipeline | both apps | Azure AI Search data plane, API 2026-04-01 |
| `DOCCHAT_OPENAI_ENDPOINT` | out: `https://<account>.openai.azure.com` while `openAi.enabled` is `true`; `https://<stub default host name>` while `modelStub.enabled` is `true` | URL | account / account / the model stub | an `https://` URL | pipeline | both apps | base of the Azure OpenAI GA v1 API (`{endpoint}/openai/v1/`); in `load` it resolves to the stub's private endpoint |
| `DOCCHAT_OPENAI_EMBEDDING_MODELS` | env: `openAi.embeddingModels`, serialised as below | JSON array | the one entry below, in every environment | 1 to 8 entries | pull request | both apps (`EmbeddingModelCatalog`) | the embedding catalogue: models, deployments, dimensions and encodings a new physical index can use |

**`DOCCHAT_OPENAI_EMBEDDING_MODELS`.** `main.bicep` serialises each `openAi.embeddingModels` entry (section 4.4) as a compact JSON object with exactly these eight members: `model`, `modelVersion` (from `version`), `queryDeployment` (`embed-query-<key>`), `ingestionDeployment` (`embed-ingest-<key>`), `dimensions` (from `allowedDimensions`), `defaultDimensions`, `maxInputTokens` and `encoding`. The entry's `key`, both deployment types and both capacities stay in Bicep. The default value:

```json
[{"model":"text-embedding-3-large","modelVersion":"1","queryDeployment":"embed-query-te3-large-v1","ingestionDeployment":"embed-ingest-te3-large-v1","dimensions":[256,1024,3072],"defaultDimensions":1024,"maxInputTokens":8192,"encoding":"cl100k_base"}]
```

`EmbeddingModelCatalog` refuses an entry with any other member, a `defaultDimensions` that isn't one of its `dimensions`, or an `encoding` other than `o200k_base` or `cl100k_base`. A physical index copies the model, version, dimensions, encoding, input limit and both deployment names into its own row when it is created, so a later change of this setting never alters an existing index (`14-ingestion.md` section 8.2).

### 2.4 Settings on the API only

None is secret. A change takes effect when the deploy restarts the API.

| Name | Source | Type | Default (`dev` / `prod` / `load`) | Allowed | Changed by | Read by | Purpose |
|---|---|---|---|---|---|---|---|
| `DOCCHAT_AUTH_TENANT_ID` | out: `tenant().tenantId` | UUID | the tenant | lower-case UUID | pipeline | token validation | expected `tid`; issuer and signing-key URLs |
| `DOCCHAT_AUTH_API_CLIENT_ID` | graph: `apiAppId` | UUID | the API registration's `appId` | distinct from the web and test client IDs | pipeline | token validation | expected `aud` |
| `DOCCHAT_AUTH_WEB_CLIENT_ID` | graph: `webAppId` | UUID | the web registration's `appId` | distinct from the API and test client IDs | pipeline | token validation | allowed `azp` |
| `DOCCHAT_AUTH_TEST_CLIENT_ID` | graph: `testAppId` | UUID or empty | the test registration's `appId` / empty / the test registration's `appId` | empty in `prod` (start-up check, section 6.1); set only in `dev` and `load` | pipeline | token validation | allowed `azp` of the test client in non-production environments |
| `DOCCHAT_API_WORKERS` | map: `{B1: 1, P0v3: 1, P1v3: 2, P2v3: 4}` over `api.sku` | integer | 1 / 2 / 2 | 1 to 16 | pipeline (follows `api.sku`) | `apps/api/gunicorn.conf.py` | Gunicorn worker count, one per vCPU |
| `DOCCHAT_API_GRACEFUL_TIMEOUT_SECONDS` | def | integer, s | 110 | 30 to 110 | pull request | `apps/api/gunicorn.conf.py` | Gunicorn `graceful_timeout`, inside App Service's 120-second stop window; the chat drain ends running turns 90 s after the stop signal (`12-chat-pipeline.md` section 14.8) |
| `DOCCHAT_API_WORKER_TIMEOUT_SECONDS` | def | integer, s | 120 | 30 to 120 | pull request | `apps/api/gunicorn.conf.py` | Gunicorn `timeout`, the worker heartbeat, which never limits a request under the Uvicorn worker |
| `DOCCHAT_COSMOS_ENDPOINT` | out: the account's `documentEndpoint` | URL | the environment's account | `https://<account>.documents.azure.com:443/` | pipeline | the conversation, memory and usage stores; `docchat-ops` reads the azd value | Cosmos DB endpoint |
| `DOCCHAT_COSMOS_DATABASE` | const | string | `docchat` | `docchat` | pipeline | the API; `docchat-ops` | Cosmos DB database name |
| `DOCCHAT_UPLOAD_MAX_BYTES` | def | integer, bytes | 52,428,800 | 1,048,576 to 209,715,200 | pull request | `UploadSettings` | per-file upload limit (50 MiB) |
| `DOCCHAT_UPLOAD_MAX_FILES_PER_BATCH` | def | integer | 100 | 1 to 100, the OpenAPI `maxItems` | pull request | `UploadSettings` | files per preflight and submit batch; named in the `batch_too_large` message |
| `DOCCHAT_UPLOAD_CONCURRENT_STREAMS_PER_WORKER` | def | integer | 4 | 1 to 16 | pull request | `UploadSettings` | upload streams per worker process |
| `DOCCHAT_SEARCH_MAX_INDEXES` | map: `{basic: 15, standard: 50}` over `search.sku` | integer | 15 / 50 / 50 | 2 to 200 | pipeline (follows `search.sku`) | `UploadSettings` | the index-capacity check before a physical index is created (`14-ingestion.md` section 8.3) |
| `DOCCHAT_SEARCH_TIMEOUT_SECONDS` | def | integer, s | 10 | 2 to 30 | pull request | the per-index search clients | read timeout of each search request |
| `DOCCHAT_OPENAI_CHAT_DEPLOYMENT` | env: `openAi.deployments.chat.name` | string | `chat` | 2 to 64 characters of letters, digits, `-`, `_` and `.` | pull request | the answer call | chat deployment name |
| `DOCCHAT_OPENAI_CHAT_MODEL` | env: `openAi.deployments.chat.model` | string | `gpt-5.6-sol` | the `pricing.model_rates` key form `^[a-z0-9][a-z0-9.\-]{0,63}$`, and a model of the capability table | pull request | pricing, the turn record | the chat model's name |
| `DOCCHAT_OPENAI_CHAT_MODEL_VERSION` | env: `openAi.deployments.chat.version` | string | `2026-07-09` | non-empty | pull request | the turn record | the pinned chat model version |
| `DOCCHAT_OPENAI_CHAT_ENCODING` | env: `openAi.deployments.chat.encoding` | enum | `o200k_base` | `o200k_base`, `cl100k_base` | pull request | prompt assembly | token counting for the answer prompt |
| `DOCCHAT_OPENAI_CHAT_REASONING_EFFORT` | def | enum | `low` | `none`, `low`, `medium`, `high`, limited to the chat model's row of the capability table (`12-chat-pipeline.md` section 10.7) | pull request | the answer call | reasoning effort of the answer |
| `DOCCHAT_OPENAI_CHAT_PROMPT_CACHE` | def | enum | `disabled` | `disabled` (sends `prompt_cache_options.mode = "explicit"` with no breakpoints), `automatic` (sends no cache parameter; required for models before GPT-5.6) | pull request | the answer call | prompt-cache control |
| `DOCCHAT_OPENAI_SMALL_DEPLOYMENT` | env: `openAi.deployments.chatSmall.name` | string | `chat-small` | as the chat deployment name | pull request | the rewrite, search-passage, follow-ups, summary and memory calls | chat-small deployment name |
| `DOCCHAT_OPENAI_SMALL_MODEL` | env: `openAi.deployments.chatSmall.model` | string | `gpt-5.6-terra` | as the chat model | pull request | pricing, the turn record | the chat-small model's name |
| `DOCCHAT_OPENAI_SMALL_MODEL_VERSION` | env: `openAi.deployments.chatSmall.version` | string | `2026-07-09` | non-empty | pull request | the turn record | the pinned chat-small model version |
| `DOCCHAT_OPENAI_SMALL_ENCODING` | env: `openAi.deployments.chatSmall.encoding` | enum | `o200k_base` | `o200k_base`, `cl100k_base` | pull request | the small calls, the summary and memory calls | token cuts and estimates for chat-small inputs |
| `DOCCHAT_OPENAI_SMALL_REASONING_EFFORT` | def | enum | `none` | `none`, `low`, `medium`, `high`, limited to the chat-small model's row of the capability table | pull request | the rewrite, search-passage and follow-ups calls | reasoning effort of the small chat-path calls |
| `DOCCHAT_OPENAI_SMALL_PROMPT_CACHE` | def | enum | `disabled` | as `DOCCHAT_OPENAI_CHAT_PROMPT_CACHE` | pull request | every chat-small call | prompt-cache control |
| `DOCCHAT_OPENAI_CONNECT_TIMEOUT_SECONDS` | def | integer, s | 10 | 1 to 30 | pull request | the API's `AsyncOpenAI` client | connect timeout |
| `DOCCHAT_OPENAI_READ_TIMEOUT_SECONDS` | def | integer, s | 60 | 10 to 120 | pull request | the API's `AsyncOpenAI` client | longest silence between chunks, or before a response |
| `DOCCHAT_CONTENTSAFETY_ENDPOINT` | out: `https://<account>.cognitiveservices.azure.com` | URL | the environment's Content Safety account, a real account in every environment including `load` | an `https://` URL | pipeline | source screening and fact screening (`shield_user_prompt`) | Prompt Shields, API 2024-09-01 |
| `DOCCHAT_CONTENTSAFETY_TIMEOUT_SECONDS` | def | integer, s | 5 | 1 to 15 | pull request | Prompt Shields calls | per-call timeout |

The ops-config values the API reads at run time are in section 3. The reasoning-effort and prompt-cache settings are app settings, not ops config, because a value a deployment refuses must fail at start-up or at the post-deploy model check, never on a user's turn.

### 2.5 Settings on the ingestion app only

None is secret. A change takes effect when the deploy restarts the function app.

| Name | Source | Type | Default (`dev` / `prod` / `load`) | Allowed | Changed by | Read by | Purpose |
|---|---|---|---|---|---|---|---|
| `DOCCHAT_OPENAI_TABLE_SUMMARY_DEPLOYMENT` | env: `openAi.deployments.tableSummary.name` | string | `ingest-summary` | 2 to 64 characters of letters, digits, `-`, `_` and `.` | pull request | the table-summary call (`14-ingestion.md` section 12.2) | table-summary deployment name |
| `DOCCHAT_OPENAI_TABLE_SUMMARY_ENCODING` | env: `openAi.deployments.tableSummary.encoding` | enum | `o200k_base` | `o200k_base`, `cl100k_base` | pull request | the table-summary call | token counts of summary inputs and outputs |
| `DOCCHAT_DOCINTEL_ENDPOINT` | out: `https://<account>.cognitiveservices.azure.com` | URL | the environment's Document Intelligence account | its custom subdomain | pipeline | the layout calls | Document Intelligence, API 2024-11-30 |
| `DOCCHAT_INGESTION_PARSE_MEMORY_LIMIT_MB` | def | integer, MB | 2,048 | 512 to 6,144 | pull request | the parsing runner | memory limit of one parsing child, the `convert` child with every LibreOffice process it starts included |
| `DOCCHAT_INGESTION_PARSE_TIMEOUT_SECONDS` | def | integer, s | 1,200 | 60 to 2,400 | pull request | the parsing runner | wall-clock limit of one `partition` or `render` child |
| `DOCCHAT_INGESTION_PARSE_SLOTS_PER_PROCESS` | def | integer | 1 | 1 to 4, and `FUNCTIONS_WORKER_PROCESS_COUNT` × this value at most 8, because each slot holds two of the 16 users of the parse-user pool (`14-ingestion.md` section 15.6 item 11; start-up refusal `parse_pool_exceeded`, section 6.1) | pull request | the parsing runner | parse slots per worker process, each running one parsing child and its verifier |
| `DOCCHAT_INGESTION_QUEUE__queueServiceUri` | out: the data account's queue endpoint | URL | the environment's data account | the queue endpoint | pipeline | the Functions runtime | connection of the `ingestion-requests` queue trigger |
| `DOCCHAT_INGESTION_QUEUE__credential` | const | string | `managedidentity` | that value | pipeline | the Functions runtime | identity-based trigger connection |
| `DOCCHAT_INGESTION_QUEUE__clientId` | out: the ingestion identity's `clientId` | UUID | `id-docchat-ingest-<env>`'s client ID | lower-case UUID | pipeline | the Functions runtime | the user-assigned identity of the trigger |

### 2.6 Settings on the model stub

The model stub (`docchat_model_stub`) is the OpenAI-compatible test service that both apps reach only because their endpoint setting points at it: locally and in CI through `compose.yaml` (host port 8091 to container port 8000), and in `load` as the site `app-docchat-stub-load-<token>` behind a private endpoint. Production code holds no reference to it. It runs `uvicorn docchat_model_stub.app:app --host 0.0.0.0 --port 8000 --workers ${DOCCHAT_STUB_WORKERS}` and reads every setting below once at start through `StubSettings`. Local values are `StubSettings`' defaults, which `compose.yaml` uses; `load` values come from the `modelStub` key of `infra/config/app-settings.json`. None is secret; each is changed by pull request, effective when the stub restarts.

| Name | Type | Local | `load` | Allowed | Purpose |
|---|---|---|---|---|---|
| `DOCCHAT_STUB_WORKERS` | integer | 1 | 2 | 1 to 8 | Uvicorn worker processes |
| `DOCCHAT_STUB_RECORD` | bool | `true` | `false` | `true`, `false` | records each request for the tests that read them |
| `DOCCHAT_STUB_FIRST_TOKEN_DELAY_MS` | integer, ms | 300 | 800 | 0 to 60,000 | delay before the first content chunk of a streamed answer |
| `DOCCHAT_STUB_TOKEN_DELAY_MS` | integer, ms | 10 | 25 | 0 to 5,000 | delay per word of a streamed answer |
| `DOCCHAT_STUB_TOKENS_PER_CHUNK` | integer | 1 | 2 | 1 to 16 | words per content chunk |
| `DOCCHAT_STUB_ANSWER_TOKENS` | integer | 60 | 400 | 1 to 4,000 | words per streamed answer |
| `DOCCHAT_STUB_TEXT_TOKENS` | integer | 40 | 120 | 1 to 4,000 | words per plain-text answer |
| `DOCCHAT_STUB_JSON_DELAY_MS` | integer, ms | 20 | 300 | 0 to 60,000 | delay of a non-streamed answer |
| `DOCCHAT_STUB_EMBEDDING_DELAY_MS` | integer, ms | 5 | 40 | 0 to 10,000 | delay of an embeddings answer |
| `DOCCHAT_STUB_THROTTLE_EVERY_N` | integer | 0 (off) | 20 | 0 to 1,000 | answers every N-th request with `429` |
| `DOCCHAT_STUB_THROTTLE_DEPLOYMENTS` | comma-separated list | empty (every deployment) | `embed-ingest-te3-large-v1,ingest-summary` | deployment names | the deployments the injected `429` applies to |
| `DOCCHAT_STUB_RETRY_AFTER_MS` | integer, ms | 200 | 500 | 0 to 60,000 | `retry-after-ms` header of an injected `429` |
| `DOCCHAT_STUB_SOURCE_ID_PATTERN` | regular expression | `\[\[BEGIN SOURCE (S\d{1,2}) ` (ending in one space) | the same | a regular expression with exactly one capture group | finds the chat pipeline's source IDs in a prompt, so the stub's answer carries `[S<n>]` markers |

In `load` the deployment-name settings of both apps keep their usual values (`chat`, `chat-small`, `ingest-summary`, `embed-query-te3-large-v1`, `embed-ingest-te3-large-v1`), because the stub keys its statistics and its throttling by them.

### 2.7 Platform settings

Settings whose names the platform defines. None is secret. A change takes effect when the deploy restarts the site.

| Name | Sites | Source and value (`dev` / `prod` / `load`) | Read by | Purpose |
|---|---|---|---|---|
| `WEBSITE_AUTH_AAD_ALLOWED_TENANTS` | API | out: the tenant ID, one lower-case GUID | App Service authentication | refuses tokens of every other tenant before a request reaches the app |
| `AzureWebJobsStorage__blobServiceUri` | ingestion | out: the host account's blob endpoint | Functions host | the host account that holds the Durable task hub state and the leases |
| `AzureWebJobsStorage__queueServiceUri` | ingestion | out: the host account's queue endpoint | Functions host | host storage |
| `AzureWebJobsStorage__tableServiceUri` | ingestion | out: the host account's table endpoint | Functions host | host storage |
| `AzureWebJobsStorage__credential` | ingestion | const: `managedidentity` | Functions host | identity-based host storage |
| `AzureWebJobsStorage__clientId` | ingestion | out: the ingestion identity's `clientId` | Functions host | the user-assigned identity of host storage |
| `FUNCTIONS_EXTENSION_VERSION` | ingestion | const: `~4` | Functions host | runtime version |
| `FUNCTIONS_WORKER_RUNTIME` | ingestion | const: `python` | Functions host | worker language |
| `FUNCTIONS_WORKER_PROCESS_COUNT` | ingestion | map `{EP1: 1, EP2: 2, EP3: 4}` over `ingestion.sku`: 1 / 2 / 2; allowed 1 to 10 | Functions host | Python worker processes per instance, one parsing child each |
| `AzureFunctionsJobHost__extensions__durableTask__maxConcurrentActivityFunctions` | ingestion | map `{EP1: 2, EP2: 4, EP3: 8}`: 2 / 4 / 4; allowed 1 to 32 | Functions host | two activity slots per worker process, so the dispatcher's activities never queue behind a parse |
| `AzureFunctionsJobHost__extensions__durableTask__maxConcurrentOrchestratorFunctions` | ingestion | map `{EP1: 2, EP2: 4, EP3: 8}`: 2 / 4 / 4; allowed 1 to 32 | Functions host | orchestrator ceiling per instance |
| `AzureFunctionsJobHost__extensions__durableTask__hubName` | ingestion | const `docchat<env>`: `docchatdev` / `docchatprod` / `docchatload`; letters and digits | Functions host | the Durable task hub name, set only here and never in `host.json` |
| `WEBSITES_ENABLE_APP_SERVICE_STORAGE` | API, ingestion, stub | const: `false` | App Service | no shared `/home` storage mounted into the containers |
| `WEBSITES_PORT` | API, stub | const: 8000 | App Service | the port Gunicorn (API) and Uvicorn (stub) bind on `0.0.0.0` |
| `WEBSITES_CONTAINER_START_TIME_LIMIT` | API, ingestion, stub | const, seconds: 300 (API), 900 (ingestion), 300 (stub) | App Service | how long a container may take to start |
| `WEBSITES_CONTAINER_STOP_TIME_LIMIT` | API | const: 120 s, the platform maximum | App Service | the stop window inside which Gunicorn's 110-second graceful timeout ends |
| `WEBSITE_HEALTHCHECK_MAXPINGFAILURES` | API, stub | const: 3 | App Service | failed `/healthz` probes before an instance is replaced |

### 2.8 Content-share fallback 1 (absent in the first version)

The ingestion app runs on Elastic Premium with no Azure Files content share. If the stage-0 content-share spike fails (`83-infrastructure.md`), the pre-chosen fallback 1 adds exactly these settings to the ingestion app, following the runbook "Content-share fallback activation" (`86-operations.md`):

| Name | Value | Secret | Read by | Purpose |
|---|---|---|---|---|
| `WEBSITE_CONTENTAZUREFILECONNECTIONSTRING` | Key Vault reference `@Microsoft.KeyVault(SecretUri=https://<kv>.vault.azure.net/secrets/ingestion-content-share-connection)` | **yes**: the secret holds `DefaultEndpointsProtocol=https;AccountName=<host account>;AccountKey=<key>;EndpointSuffix=core.windows.net`, written by Bicep from `listKeys()` | Functions host | content-share connection |
| `WEBSITE_CONTENTSHARE` | `func-docchat-ingest-<env>` | no | Functions host | share name (quota 100 GiB) |
| `WEBSITE_SKIP_CONTENTSHARE_VALIDATION` | `1` | no | Functions host | skips the platform's share validation behind the private endpoint |

With them the site sets `keyVaultReferenceIdentity` to the ingestion identity's resource ID and `outboundVnetRouting.contentShareTraffic` and `outboundVnetRouting.allTraffic` to `true`; the ingestion identity gets Key Vault Secrets User on that one secret; the host key is rotated every 90 days by the runbook "Rotate the host-account key (content-share fallback 1 only)". The rest of fallback 1 (the host account's shared-key exception, the `file` private endpoint, the NAT gateway) is in `83-infrastructure.md`.

### 2.9 Image environment

Set by `ENV` in the Dockerfiles, so no deployment can change them; a change ships as a new image. None is secret.

| Name | Images | Value | Purpose |
|---|---|---|---|
| `DOCCHAT_BUILD_COMMIT` | API, ingestion, model stub | `ARG DOCCHAT_BUILD_COMMIT`, 40 lower-case hexadecimal characters passed by the `build` job, copied to `ENV` | `service.version` of telemetry (`AppVersion`) |
| `PYTHONDONTWRITEBYTECODE` | API, model stub | `1` | no bytecode files written at run time |
| `PYTHONUNBUFFERED` | API, model stub | `1` | unbuffered standard output and error |
| `TIKTOKEN_CACHE_DIR` | API, ingestion | `/opt/docchat/tiktoken`, filled with `o200k_base` and `cl100k_base` at build time | encodings load with no network access |
| `HF_HOME` | ingestion | `/opt/docchat/models/hf` | the pinned layout models, fetched at build time |
| `HF_HUB_OFFLINE` | ingestion | `1` | no model download at run time |
| `HF_HUB_DISABLE_TELEMETRY` | ingestion | `1` | no library telemetry |
| `DO_NOT_TRACK` | ingestion | `true` | no library telemetry |
| `SCARF_NO_ANALYTICS` | ingestion | `true` | no library telemetry |
| `UNSTRUCTURED_DEFAULT_MODEL_INITIALIZE_PARAMS_JSON_PATH` | ingestion | `/opt/docchat/models/layout/params.json` | the layout model's local parameter file |
| `OMP_NUM_THREADS` | ingestion | `1` | one OpenMP thread per parsing child |
| `AzureWebJobsScriptRoot` | ingestion | `/home/site/wwwroot` | the Functions app root in the image |
| `AzureFunctionsJobHost__Logging__Console__IsEnabled` | ingestion | `true` | host console logging |

The ingestion image's `host.json` is part of the image (`14-ingestion.md` section 15.9); it names no task hub.

### 2.10 Values set inside the process, and values never set

1. `configure_telemetry` sets `OTEL_TRACES_SAMPLER` = `microsoft.fixed_percentage`, `OTEL_TRACES_SAMPLER_ARG` = the value of `DOCCHAT_TELEMETRY_SAMPLING_RATIO` and `OTEL_PROPAGATORS` = `tracecontext` in its own process environment before it configures the distro. None of the three is an app setting, so one setting decides the sampling ratio. The sampler doesn't follow the caller's sampled flag.
2. `PYTHON_APPLICATIONINSIGHTS_ENABLE_TELEMETRY` stays unset in every image and on every site, because the Functions worker's own auto-configuration would bypass the scrubbing processors.
3. `WEBSITE_RUN_FROM_PACKAGE` is never set; `WEBSITE_CONTENTAZUREFILECONNECTIONSTRING`, `WEBSITE_CONTENTSHARE` and `WEBSITE_SKIP_CONTENTSHARE_VALIDATION` are absent unless fallback 1 applies (section 2.8).
4. The Functions host's OpenTelemetry mode is a preview feature and stays off; the distro in the worker process exports telemetry.

### 2.11 Local settings

Values that exist only on a developer machine or in a CI job, never in Azure.

| Name | Where | Type | Default | Allowed | Secret | Changed by | Read by | Purpose |
|---|---|---|---|---|---|---|---|---|
| `DOCCHAT_LOCAL_EMULATORS` | the developer's `.env` | bool | `false` | `true` is refused when `WEBSITE_SITE_NAME` or `WEBSITE_INSTANCE_ID` is set (section 6.3) | no | the developer | the API, the ingestion app (`LocalSettings`) | local-emulator mode |
| `DOCCHAT_LOCAL_SQL_SA_PASSWORD` | the developer's `.env` (gitignored) | string | written by `docchat-ops --env local local init`; a CI job generates its own | 24 characters meeting SQL Server's complexity rules | **yes** (local only) | `docchat-ops local init`, the developer, a CI job | `compose.yaml` (`MSSQL_SA_PASSWORD`), local runs | the local SQL Server's `sa` password |
| `DOCCHAT_DEV_API_TARGET` | the developer's shell when running `vite dev` | URL origin | `http://127.0.0.1:8000` | an `http:` or `https:` origin: a local API process, or the deployed `dev` API's default host | no | the developer | `vite.config.ts` only, never the bundle | where the dev server proxies `/v1` and `/healthz` |
| `apps/web/config.local.json` | the developer's working copy | JSON file | `apiBaseUrl` = `http://localhost:5173` and the developer's tenant, client and scope | the `config.json` schema (section 5.1) | no | the developer | the dev server, which answers `/config.json` from it | the web client's configuration in development |

**Local-emulator mode.** When `DOCCHAT_LOCAL_EMULATORS` is `true`, `docchat_core.clients` and the SQL layer use the `compose.yaml` services instead of Azure: SQL Server at `127.0.0.1,14330` with SQL authentication as `sa` and `DOCCHAT_LOCAL_SQL_SA_PASSWORD` (`Encrypt=yes;TrustServerCertificate=yes`); `UseDevelopmentStorage=true` for Blob and Queue (Azurite on ports 10000 to 10002); the Cosmos DB emulator at `http://127.0.0.1:8081` with its published key; and `http://` model and screening endpoints on loopback (the model stub at `http://127.0.0.1:8091`). `DOCCHAT_CORE_ENVIRONMENT` is `local` in the developer's `.env`, which selects `AzureCliCredential` for any real Azure resource. Every other setting a local process needs is read under the same name from the developer's `.env`. Tests construct their clients directly and never set the flag.

**Never committed.** The forbidden-file check of CI fails a pull request that tracks a file named exactly `.env`, anything under `.azure/`, or `apps/web/config.local.json`, even when it was forced past `.gitignore`.

## 3. Ops config (`app.ops_config`)

Runtime operational values live in SQL as typed rows, so an operator can change a limit, a quota or ingestion parallelism within 60 seconds without a deploy, and every change is audited. None is secret. ADR 0028 records the mechanism.

### 3.1 The mechanism

1. **The table.** `app.ops_config` (`80-data-model.md` section 2.7) holds exactly one row per key of this section: `config_key` (the dotted key), `value_json` and `default_json` (each `{"value": <typed value>}`, so `ISJSON` stays meaningful for scalars), `change_reason` (`NULL` while seeded, then the operator's 10-to-500-character reason), `updated_at`, `updated_by` (`NULL` while seeded, then the operator's `oid`) and `row_version`. Grants: `docchat_api` and `docchat_ingestion` `SELECT`; `docchat_config_operator` `SELECT` and `UPDATE`, never `INSERT` or `DELETE`; `docchat_user_data_operator` `SELECT`. Only migrations, run by the deploy identity, insert or delete rows.
2. **Area models.** The area of a key is the text before its first dot. `docchat_core.opsconfig.OpsConfig` is a root model with one Pydantic sub-model per area. Every field is aliased to its full dotted key, carries its range and has no code default, because the database is the source; every sub-model uses `extra="forbid"` and holds its cross-field rules. Pydantic parses each JSON value as typed data; nothing is ever evaluated.

| Area | Model | Module | Keys | Registered by | Specified in |
|---|---|---|---|---|---|
| `api` | `ApiOps` | `docchat_core.opsconfig` | 1 | API | `11-api.md` section 1.11 |
| `chat` | `ChatOps` | `docchat_core.opsconfig` | 6 | API | `12-chat-pipeline.md` section 18 |
| `retrieval` | `RetrievalOps` | `docchat_core.opsconfig` | 6 | API | `12-chat-pipeline.md` sections 6 and 7 |
| `followups` | `FollowupsOps` | `docchat_core.opsconfig` | 5 | API | `12-chat-pipeline.md` section 13 |
| `ratelimit` | `RateLimitOps` | `docchat_core.opsconfig` | 4 | API | `12-chat-pipeline.md` sections 3 and 16 |
| `quota` | `QuotaOps` | `docchat_core.opsconfig` | 2 | API | `12-chat-pipeline.md` section 3 |
| `pricing` | `PricingOps` | `docchat_core.opsconfig` | 2 | API | `12-chat-pipeline.md` section 15 |
| `conversations` | `ConversationsOps` | `docchat_core.opsconfig` | 1 | API | `13-conversations-and-memory.md` section 1.9 |
| `summary` | `SummaryOps` | `docchat_core.opsconfig` | 9 | API | `13-conversations-and-memory.md` section 6 |
| `memory` | `MemoryOps` | `docchat_core.opsconfig` | 11 | API | `13-conversations-and-memory.md` section 7 |
| `feedback` | `FeedbackOps` | `docchat_core.opsconfig` | 1 | API | `13-conversations-and-memory.md` section 8.4 |
| `ingestion` | `IngestionOpsConfig` | `docchat_core.ingestion.settings` | 25 | ingestion app | `14-ingestion.md` section 18.2 |

That is 73 keys in 12 areas.

3. **Start-up.** Each process registers the areas it reads (the API the first eleven, the ingestion app `ingestion`). `OpsConfigStore` reads every row with one statement, `SELECT config_key, value_json FROM app.ops_config` (the table stays at a few hundred rows), groups the rows by area, drops rows of unregistered areas and keys the area's model doesn't declare (an expand migration ships a key before the code that reads it), and validates each registered area from the `value` members of its keys. A missing key or an invalid value in a registered area raises, and the process exits with a non-zero code before it serves; App Service restarts the container. The start-up log record names the area and the key, never the value.
4. **One snapshot per unit of work.** `store.snapshot()` returns the immutable root model while it is younger than 60 seconds. The first access after that refreshes it under a lock, and concurrent callers wait for that one refresh. Callers take one snapshot per request, one per turn on the chat path (follow-ups and citations included), and the ingestion dispatcher one per wake, whose values each item receives in its sub-orchestration input, so one item runs with one consistent set even when an operator changes a value mid-run.
5. **Refresh failures.** A refresh replaces each registered area only when it validates. An area that fails keeps its last valid sub-model, increments `docchat.config.invalid` (attributes `area`, `key`) and emits `ops_config_invalid` (a custom event and an error log record naming the area and key). A refresh that can't reach SQL keeps the snapshot, logs `ops_config_unreachable` (warning), increments `docchat.opsconfig.refresh_failures` (`reason` `unreachable`) and allows the next attempt no sooner than 15 seconds later. Each refresh is one span `docchat.opsconfig.refresh` with `docchat.opsconfig.outcome` (`ok`, `partial` or `unreachable`) and `docchat.opsconfig.invalid_areas`. The alert `ops-config-invalid` fires on any `docchat.config.invalid` (`85-observability.md`).
6. **No timer.** Nothing refreshes the snapshot on a schedule, so the API adds no periodic SQL query and a paused `dev` database stays paused. A committed change is therefore live within 60 seconds whenever SQL is reachable and requests arrive.
7. **The dispatcher.** `load_dispatch_settings` reads the snapshot at each wake. The dispatcher carries its last valid snapshot across `continue_as_new` in its own input, so the rule doesn't depend on which worker runs the activity. A dispatcher with no valid snapshot (the first wake of a new instance while the stored value is invalid) sweeps and claims nothing, reports `configValid: false` in its custom status and wakes after 60 seconds to read the snapshot once more, so nothing runs on a value the model refuses (`14-ingestion.md` section 5.2).

### 3.2 Seeding and migrations

1. The Alembic revision that ships a key inserts its row with `value_json` = `default_json` = `{"value": <default>}`, using the default this section gives. The first revision, `0001_initial_schema`, seeds all 73 keys of the first version, in one function per owning area (`80-data-model.md` section 2.24): `api.*` for the API conventions; `chat.*`, `retrieval.*`, `followups.*`, `ratelimit.*`, `quota.*` and `pricing.*` for the chat pipeline; `conversations.*`, `summary.*`, `memory.*` and `feedback.*` for the conversation store; and `ingestion.*` for ingestion. A key added after the first version is seeded by the expand revision that ships it, written in its owning area's function.
2. A revision that changes a default updates `default_json`, and `value_json` only where it still equals the old default, so an operator's choice survives the change.
3. A key is retired in two steps: the release that stops reading it removes the field from its area model (the loader then ignores the row), and a later contract revision deletes the row (`80-data-model.md` section 2.24).
4. A key is renamed as a new key plus a retired key, never in place.
5. The migration test applies every revision to an empty database and compares the rows of `app.ops_config` with the 73 keys, types and defaults of sections 3.4 to 3.8; a missing, extra or differing row fails it.

### 3.3 Changing a value

1. **Who.** An operator who is a just-in-time member of `sg-docchat-<env>-config-operators`, whose contained database user is in the role `docchat_config_operator`. Membership is granted for at most 4 hours by a second person: through PIM for Groups when the tenant has Entra ID P2 or ID Governance (eligible membership, activation approved by a group owner with a justification and a ticket number), otherwise by a group owner who isn't the operator running `docchat-ops --env <env> jit grant --level config`. Each group has at least 2 named owners, the environment's approvers. The operator runs the command from a machine whose public IP is in the environment's operator allowlist, under their own Entra identity (`AzureCliCredential`, SQL token for `https://database.windows.net/.default`).
2. **`uv run docchat-ops --env <env> config show [--key <key>]`** prints each key's value, default, last change time and actor ID. It reads no personal data and takes no `--ticket` or `--reason`.
3. **`uv run docchat-ops --env <env> config set --key <key> --value '<JSON value>' --ticket <id> --reason '<10 to 500 characters>'`** refuses a key outside this catalogue; validates the value with the area's model against the area's current values, cross-field rules included; prints the old and new values; in `prod` asks the operator to type the environment name; then, in one transaction, updates the row `WHERE config_key = :key AND row_version = :rv`, storing `change_reason` and `updated_by`, and inserts the audit row `ops_config.set` (`actor_type` `operator`; metadata `area`, `previous`, `new`, `ticket`). Zero rows updated means someone else changed the row since it was read: exit 6, nothing written. The output says the value is live within 60 seconds.
4. **`uv run docchat-ops --env <env> config reset --key <key> --ticket <id> --reason '<10 to 500 characters>'`** sets `value_json` to `default_json` under the same checks and inserts `ops_config.reset` (metadata `area`, `previous`, `ticket`).
5. **Exit codes:** 0 done; 2 usage error or validation failure (unknown key, out-of-range value, a broken cross-field rule, a reason outside 10 to 500 characters), nothing written; 3 access denied (no sign-in, no just-in-time membership so SQL refuses the login, or a service-principal caller of `config set` or `config reset` in `dev` or `prod`); 6 conflict; 7 database unreachable (allowlist or network); 1 anything else, printed with the run's correlation ID.
6. **Linked checks first.** In `prod`, a change to any key in either formula of the capacity invariant (section 3.6) is first evaluated with `uv run docchat-ops --env prod chat capacity-check --set <key>=<JSON value>`, which writes nothing; a share above 25% means the deployment's capacity is raised first (section 4.3). Raising `ingestion.max_parallel_items` follows the ingestion runbook's check of the Azure OpenAI and Document Intelligence quotas (`86-operations.md`).
7. **One exception, in `load` only.** The `load` deploy identity, a standing member of that environment's SQL Entra admin group, applies the load-test values of section 3.9 from the load workflow's jobs `seed-config`, `stream-reset`, `s4-set1-config`, `s4-set2-config` and `s4-reset`, which run in the GitHub environment `load`, hold no test token and open the SQL firewall only for themselves; each change writes an audit row with `actor_type` `operator` and that identity's object ID, because a load run can't wait for a person between scenarios.
8. **Never through the product.** No API operation and no admin screen reads or writes `app.ops_config`, apart from the two values `getCurrentUser` exposes (`memory.enabled` and `feedback.comments_enabled`, section 3.7).

The runbook "Change an ops-config value" (`86-operations.md`) wraps these steps: ticket, membership, `config show`, `config set`, a 60-second wait and a check of the owning dashboard tile, revocation (`jit revoke` or PIM expiry), and the undo (`config reset`, or `config set` with the previous value).

### 3.4 Area `api` (`ApiOps`)

| Key | Type | Default | Allowed range | Read by | Purpose |
|---|---|---|---|---|---|
| `api.write_requests_per_minute` | integer | 120 | 10 to 1,200 | API | per-user limit on counted write requests in a sliding minute (`11-api.md` section 1.11) |

### 3.5 Areas of the chat pipeline

Models `ChatOps`, `RetrievalOps`, `FollowupsOps`, `RateLimitOps`, `QuotaOps` and `PricingOps`; every key is read by the API. Cross-field rules, each of which makes the whole area invalid when broken: `chat.answer_max_completion_tokens` ≤ `chat.context_token_budget` − 8,000; `retrieval.keep_count` ≤ `retrieval.candidate_count`; `retrieval.min_confident_results` ≤ `retrieval.keep_count`; `quota.tokens_per_month` ≥ `quota.tokens_per_day`. No key bounds the conversation history by turn count, because the prompt budget does: `ChatOps` has no such field and no such row is seeded.

| Key | Type | Default | Allowed range | Purpose |
|---|---|---|---|---|
| `chat.context_token_budget` | integer, tokens | 40,000 | 20,000 to 200,000 | answer prompt plus completion reserve; minus `chat.answer_max_completion_tokens` (28,000 with the defaults) it is the `max_tokens` the history read `read_recent_turns` receives; also the per-request charge of the chat share (section 3.6) |
| `chat.answer_max_completion_tokens` | integer, tokens | 12,000 | 2,000 to 40,000, and at most `chat.context_token_budget` − 8,000 | the answer's reasoning plus visible tokens |
| `chat.small_call_max_completion_tokens` | integer, tokens | 800 | 200 to 8,000 | completion cap of the rewrite, search-passage and follow-ups calls |
| `chat.rewrite_history_turns` | integer | 4 | 1 to 8 | earlier turns given to the rewrite |
| `chat.rewrite_timeout_seconds` | integer, s | 8 | 2 to 20 | rewrite call budget |
| `chat.max_turn_seconds` | integer, s | 180 | 60 to 220 | turn cap; `deadlineAt` and the stream lease's expiry are the turn's receipt time plus this value plus 30 s |
| `retrieval.candidate_count` | integer | 50 | 10 to 50 | `top` and `k` of each search |
| `retrieval.keep_count` | integer | 8 | 1 to 16, and at most `retrieval.candidate_count` | sources kept after the served-chunk-set filter |
| `retrieval.reranker_threshold` | decimal | 2.0 | 0.0 to 4.0 | semantic reranker score that counts as confident |
| `retrieval.min_confident_results` | integer | 1 | 1 to `retrieval.keep_count` | confident results needed to skip the broadened search |
| `retrieval.semantic_max_wait_ms` | integer, ms | 2,000 | 700 to 10,000 | semantic ranker wait before partial results |
| `retrieval.broaden_timeout_seconds` | integer, s | 8 | 2 to 20 | search-passage call budget |
| `followups.enabled` | boolean | `true` | `true`, `false` | follow-up suggestions on or off |
| `followups.timeout_seconds` | integer, s | 5 | 2 to 10 | follow-ups call budget |
| `followups.max_count` | integer | 3 | 1 to 3 | questions kept |
| `followups.source_count` | integer | 3 | 1 to 8 | sources given to the follow-ups call |
| `followups.source_excerpt_tokens` | integer, tokens | 300 | 100 to 800 | excerpt length per source |
| `ratelimit.chat_requests_per_minute` | integer | 3 | 1 to 1,200; bounded in `prod` by the capacity invariant (section 3.6) | sliding-window limit of `createChatTurn` per user |
| `ratelimit.max_concurrent_streams` | integer | 2 | 1 to 200 | stream lease slots per user |
| `ratelimit.citation_downloads_per_minute` | integer | 30 | 1 to 500 | sliding-window limit of `getCitationContent`, `getCitationDocument` and `adminGetDocumentPdf` requests together, per user |
| `ratelimit.max_concurrent_downloads` | integer | 2 | 1 to 10 | download lease slots per user, shared by the three file operations |
| `quota.tokens_per_day` | integer, tokens | 2,000,000 | 10,000 to 1,000,000,000 | per-user token quota of a UTC day, charged by chat turns and by the after-response summary and memory calls |
| `quota.tokens_per_month` | integer, tokens | 32,000,000 | `quota.tokens_per_day` to 100,000,000,000 | per-user token quota of a UTC month |
| `pricing.currency` | string | `USD` | three upper-case letters (ISO 4217) | currency of the rates and the cost estimates |
| `pricing.model_rates` | object | `{}` | at most 20 keys; each key a model name matching `^[a-z0-9][a-z0-9.\-]{0,63}$`; each value exactly `{inputPerMillion, cachedInputPerMillion, cacheWritePerMillion, outputPerMillion}`, each a decimal string from 0 to 1000 with at most 6 decimal places | per-model token prices for the cost estimates of chat, chat-small and embedding calls (`12-chat-pipeline.md` section 15) |

The ranges serve the product's limits: a 20,000-token budget is the smallest that holds the default 12,000-token answer reserve plus the 8,000-token minimum prompt, and 200,000 keeps one turn's cost far below the chat model's 1,050,000-token context; an answer reserve of up to 40,000 tokens leaves room above the vendor's advice to reserve at least 25,000 tokens for reasoning plus output. A new model's prices are set with `config set` on `pricing.model_rates` before traffic reaches the model (runbook "Model version upgrade").

### 3.6 The capacity invariant in `prod`

The Azure OpenAI limiter counts each request's prompt tokens plus its `max_completion_tokens` against the deployment's tokens per minute (capacity units × 1,000). In `prod` both shares stay at or below 25%:

- **chat share** = `ratelimit.chat_requests_per_minute` × `chat.context_token_budget` ÷ (chat capacity × 1,000);
- **chat-small share** = `ratelimit.chat_requests_per_minute` × `S` ÷ (chat-small capacity × 1,000), where `S` is the largest chat-small charge one turn and its after-response work can cause: rewrite (300 + `summary.max_summary_tokens` + 610 × `chat.rewrite_history_turns` + 4,000 + `chat.small_call_max_completion_tokens`) + search passage (4,300 + `chat.small_call_max_completion_tokens`) + follow-ups (5,100 + (500 + `followups.source_excerpt_tokens`) × `followups.source_count` + `chat.small_call_max_completion_tokens`) + memory (6,500 + `memory.max_completion_tokens`) + summary (`summary.max_input_tokens` + 350 + `summary.max_completion_tokens`).

With the defaults `S` = 8,340 + 5,100 + 8,300 + 7,700 + 14,350 = 43,790 tokens, so the shares are 3 × 40,000 = 120,000 ≤ 125,000 (24% of 500 chat units) and 3 × 43,790 = 131,370 ≤ 132,500 (24.8% of 530 chat-small units). `docchat_core.chat.capacity.capacity_shares(ops, chat_capacity_units, small_capacity_units)` computes both. `docchat-ops chat capacity-check` reads the capacities from the azd outputs `DOCCHAT_OPENAI_CHAT_CAPACITY` and `DOCCHAT_OPENAI_SMALL_CAPACITY` (section 4.9) and the values from `app.ops_config`; `--set <key>=<JSON value>` evaluates a proposed value without writing it. It prints both shares and exits 2 when either exceeds 25% in `prod`; it exits 0 in `dev`, whose 30-unit deployments serve only developers; an empty or absent capacity output counts as absent, and the command reports it and exits 0 outside `prod` and 4 in `prod`. The `postprovision` hook runs it after the migrations and grants on every deploy: exit 2 in `prod` fails the deploy as `chat_capacity_exceeded`, any other non-zero exit as `chat_capacity_check_failed`. `load` has no Azure OpenAI account, so both capacity outputs are empty and the check exempts it. A unit test checks the seeded defaults against the capacities in `infra/env/prod.json`.

### 3.7 Areas of conversations and memory

Models `ConversationsOps`, `SummaryOps`, `MemoryOps` and `FeedbackOps`; every key is read by the API. Cross-field rules: `summary.keep_recent_tokens` < `summary.trigger_tokens` ≤ `summary.max_input_tokens`; `summary.max_completion_tokens` ≥ `summary.max_summary_tokens` + 200; `summary.lease_seconds` ≥ 3 × `summary.timeout_seconds`; `memory.lease_seconds` ≥ 3 × `memory.timeout_seconds`; `summary.reasoning_effort` and `memory.reasoning_effort` must each be in the chat-small model's row of the capability table, so `none` is accepted only while that row lists it.

| Key | Type | Default | Allowed range | Purpose |
|---|---|---|---|---|
| `conversations.max_turns_per_session` | integer | 500 | 50 to 5,000 | turns allowed in one session |
| `summary.trigger_tokens` | integer, tokens | 3,000 | 1,000 to 20,000 | unsummarised tokens that make a summary rebuild due |
| `summary.keep_recent_tokens` | integer, tokens | 1,200 | 0 to 19,000, and below `summary.trigger_tokens` | verbatim tail a rebuild keeps |
| `summary.max_input_tokens` | integer, tokens | 12,000 | 2,000 to 60,000, and at least `summary.trigger_tokens` | summary plus folded turns sent to one rebuild call |
| `summary.max_summary_tokens` | integer, tokens | 800 | 200 to 2,000 | summary length cap |
| `summary.max_completion_tokens` | integer, tokens | 2,000 | 500 to 16,000, and at least `summary.max_summary_tokens` + 200 | `max_completion_tokens` of the summary call |
| `summary.reasoning_effort` | enum | `low` | `none`, `low`, `medium`, each only while the capability table lists it for the chat-small model | reasoning effort of the summary call |
| `summary.timeout_seconds` | integer, s | 30 | 5 to 120 | per-attempt timeout of the summary call |
| `summary.lease_seconds` | integer, s | 120 | 30 to 900, and at least 3 × `summary.timeout_seconds` | rebuild lease |
| `summary.max_backoff_minutes` | integer, min | 60 | 1 to 1,440 | cap of the rebuild failure backoff |
| `memory.enabled` | boolean | `true` | `true`, `false` | platform switch for memory extraction and the memory block in prompts; exposed as `getCurrentUser.features.memoryEnabled` |
| `memory.max_facts` | integer | 50 | 1 to 200 | facts per user |
| `memory.max_tokens` | integer, tokens | 1,000 | 100 to 4,000 | token total of a user's facts |
| `memory.prompt_max_tokens` | integer, tokens | 1,300 | 200 to 5,000 | cap of the rendered memory block |
| `memory.max_operations_per_turn` | integer | 3 | 1 to 10 | most memory operations one extraction keeps |
| `memory.max_completion_tokens` | integer, tokens | 1,200 | 300 to 8,000 | `max_completion_tokens` of the memory call |
| `memory.reasoning_effort` | enum | `low` | as `summary.reasoning_effort` | reasoning effort of the memory call |
| `memory.timeout_seconds` | integer, s | 20 | 5 to 60 | per-attempt timeout of the memory call |
| `memory.lease_seconds` | integer, s | 60 | 30 to 900, and at least 3 × `memory.timeout_seconds` | extraction claim lease |
| `memory.max_attempts` | integer | 3 | 1 to 5 | failed calls before an extraction is `failed` |
| `memory.retry_window_hours` | integer, h | 24 | 1 to 168 | age after which a pending extraction expires |
| `feedback.comments_enabled` | boolean | `true` | `true`, `false` | accept free-text feedback comments; exposed as `getCurrentUser.features.feedbackComments` |

### 3.8 Area `ingestion` (`IngestionOpsConfig`)

Every key is an integer, read by the ingestion dispatcher at each wake and passed to each item (section 3.1 item 7). A change takes effect at the first wake after the dispatcher's snapshot refreshes: within 60 s plus `ingestion.active_wake_seconds` while work is outstanding, within `ingestion.idle_wake_minutes` when idle; an item already running keeps the values it started with.

| Key | Default | Allowed range | Purpose |
|---|---|---|---|
| `ingestion.max_parallel_items` | 1 | 1 to 16 | items running at once across all runs |
| `ingestion.active_wake_seconds` | 60 | 15 to 300 | dispatcher timer while work is outstanding, s |
| `ingestion.idle_wake_minutes` | 60 | 5 to 60 | dispatcher timer when idle, min; the longest wait for a run whose queue message was lost |
| `ingestion.scan_wait_minutes` | 240 | 30 to 1,440 | wait for a malware-scan result before `scan_unavailable`, min |
| `ingestion.stale_upload_hours` | 24 | 1 to 168 | age at which an `uploading` version is removed, h |
| `ingestion.orphan_pending_minutes` | 15 | 5 to 1,440 | age at which a `pending` version with no item gets one, min |
| `ingestion.cleanup_retry_minutes` | 15 | 5 to 1,440 | age at which leftover clean-up is retried, min |
| `ingestion.max_item_claims` | 3 | 1 to 10 | claims of one item before `processing_interrupted` |
| `ingestion.retry_max_attempts` | 7 | 1 to 10 | attempts per activity call |
| `ingestion.retry_first_delay_seconds` | 15 | 1 to 300 | first backoff delay, s |
| `ingestion.retry_max_delay_seconds` | 480 | 10 to 540 | backoff cap, s; no wait between attempts, `Retry-After` included, exceeds 540 s, so every wait ends inside the item lease's 10-minute margin |
| `ingestion.activity_time_budget_seconds` | 1,200 | 60 to 1,200 | working time of one Enrich, Embed or blob-purge call, s, at least 300 s inside its 1,560-second deadline |
| `ingestion.max_pages_per_document` | 2,000 | 1 to 2,000 | page cap of a version's PDF, the PDF a DOCX or PPTX is converted to included |
| `ingestion.pagewise_pages_per_extract` | 40 | 1 to 200 | PDF pages per Extract call |
| `ingestion.max_parse_seconds_per_document` | 7,200 | 900 to 43,200 | total child-process wall-clock seconds one build may spend converting and parsing |
| `ingestion.embedding_batch_max_inputs` | 64 | 1 to 2,048 | inputs per embeddings request |
| `ingestion.embedding_batch_max_tokens` | 60,000 | 8,192 to 300,000 | tokens per embeddings request; two in flight use at most 120,000 of the `prod` ingestion deployment's 250,000 tokens per minute |
| `ingestion.embedding_concurrency` | 2 | 1 to 8 | embeddings requests in flight per item |
| `ingestion.index_batch_max_documents` | 100 | 1 to 1,000 | documents per index upload batch |
| `ingestion.index_batch_max_bytes` | 8,000,000 | 1,000,000 to 15,000,000 | serialised bytes per index upload batch |
| `ingestion.table_summary_concurrency` | 4 | 1 to 16 | table-summary calls in flight per item |
| `ingestion.table_summary_max_input_tokens` | 6,000 | 500 to 20,000 | table HTML tokens sent per call |
| `ingestion.table_summary_max_output_tokens` | 300 | 50 to 500 | longest summary kept |
| `ingestion.run_retention_days` | 180 | 30 to 3,650 | age at which finished runs are deleted, days |
| `ingestion.durable_history_retention_days` | 30 | 7 to 365 | age at which finished Durable instances are purged, days |

### 3.9 Values in `load`

The load workflow's `seed-config` job sets, as the `load` deploy identity (section 3.3 item 7): `ratelimit.max_concurrent_streams` = 200, `ratelimit.chat_requests_per_minute` = 1,200, `quota.tokens_per_day` = 1,000,000,000, `quota.tokens_per_month` = 100,000,000,000 and `chat.max_turn_seconds` = 220. The turn cap of 220 s holds only while the `load` smoke job runs the deployed 200-second stream test; the job `stream-reset` then sets `chat.max_turn_seconds` back to its default 180 before the first scenario, so the scenarios run with the production turn cap and its 210-second stream-lease bound (`84-testing.md` section 7.3). Every other key keeps its default (follow-ups, summaries and memory on), so after-response work loads the system as in production. The jobs `s4-set1-config`, `s4-set2-config` and `s4-reset` set and restore `ingestion.max_parallel_items` for the ingestion-parallelism scenario, with the values `84-testing.md` gives. `dev` and `prod` run on the defaults until an operator changes a value.


## 4. Bicep parameters, environment files and delivery variables

Every value in this section is read by Bicep or by a delivery script, never by a running app except through the app settings of section 2. None is secret. A change takes effect at the next provision of the environment (the provision step of `deploy-environment.yaml`, or `azd provision`), unless a row says otherwise. `83-infrastructure.md` section 8 specifies how the files are used.

### 4.1 Runtime parameters (`infra/main.bicepparam`)

`infra/main.bicepparam` is the only parameter file of `main.bicep` (ADR 0096). It starts `using 'main.bicep'` and reads each runtime value with `readEnvironmentVariable`, which azd and `az deployment group what-if` both resolve at compile time, so one file serves azd, CI and local runs.

| Parameter | Source variable and expression | Type | Default | Allowed | Changed by | Purpose |
|---|---|---|---|---|---|---|
| `environmentName` | `readEnvironmentVariable('AZURE_ENV_NAME')` | string | none | `dev`, `prod`, `load` (`@allowed`) | pipeline (the job's environment); the operator's azd environment locally | selects `infra/env/<env>.json` |
| `apiImage` | `readEnvironmentVariable('DOCCHAT_API_IMAGE_CURRENT')` | string | none | digest reference `<login server>/docchat/api@sha256:<64 hex>` | the `preprovision` hook | keeps the running image during provisioning |
| `ingestionImage` | `readEnvironmentVariable('DOCCHAT_INGESTION_IMAGE_CURRENT')` | string | none | digest reference | the `preprovision` hook | the same, for the ingestion app |
| `modelStubImage` | `readEnvironmentVariable('DOCCHAT_MODEL_STUB_IMAGE_CURRENT', '')` | string | empty outside `load` | digest reference in `load` | the `preprovision` hook | the model stub |
| `operatorIpAllowlist` | `split(readEnvironmentVariable('DOCCHAT_OPERATOR_IP_ALLOWLIST', ''), ',')`, empty entries removed | string array | empty | at most 20 public IPv4 addresses (preflight check 9, `allowlist_invalid`) | operators, through the GitHub environment variable (runbook "Change the operator allowlist") | firewall allowlist of every resource with an IP rule |
| `resourceTokenSalt` | `readEnvironmentVariable('DOCCHAT_RESOURCE_TOKEN_SALT', '')` | string | empty | empty, or `run-<digits>` in `load` | the load workflow | fresh resource names per load run |
| `releaseSha` | `readEnvironmentVariable('DOCCHAT_RELEASE_SHA', 'local')` | string | `local` | 40 lower-case hexadecimal characters, or `local` | pipeline | tag `docchat-release-sha` on the API site |
| `opsAlertEmails` | `split(readEnvironmentVariable('DOCCHAT_OPS_ALERT_EMAILS', ''), ',')`, empty entries removed | string array | empty; `prod` refuses empty (preflight check 12, `alert_receivers_missing`) | addresses of shared mailboxes | operators, through the GitHub environment variable | receivers of `ag-docchat-<env>-ops`, the environment's one action group |
| `budgetStartDate` | `readEnvironmentVariable('DOCCHAT_BUDGET_START_DATE')`, no default | string | none: a provision without the hook fails at compile time | `yyyy-MM-01`: the existing budget's start date, or the first day of the current UTC month when the budget read answers `404` | the `preprovision` hook | the budget's start date, set once when the budget is created |

No environment carries a security mailbox: every security alert goes to the bootstrap's `ag-docchat-security` (section 4.7).

### 4.2 Environment files (`infra/env/<env>.json`)

`infra/env/dev.json`, `infra/env/prod.json` and `infra/env/load.json` are committed and changed only by a reviewed pull request. Each is typed by the exported, sealed user-defined type `environmentConfig` in `infra/types.bicep`; every nested object type is sealed too, so an undeclared member is a compile error. `main.bicep` loads all three into one typed variable, `var envConfigs {dev: environmentConfig, prod: environmentConfig, load: environmentConfig} = {dev: loadJsonContent('env/dev.json'), prod: loadJsonContent('env/prod.json'), load: loadJsonContent('env/load.json')}`, and passes `envConfigs[environmentName]` to every module, so every build checks all three files whichever environment it targets: a missing, extra or mistyped field in any of them fails `bicep build` and `bicep build-params`. The bootstrap writes the tenant-specific IDs and an operator commits them; IP addresses and personal email addresses never go in these files.

| Field | Type and allowed values | `dev` / `prod` / `load` | Read by | Purpose and when a change takes effect |
|---|---|---|---|---|
| `subscriptionId` | UUID | written by the bootstrap | Bicep, the preflight, CI | target subscription |
| `tenantId` | UUID | written by the bootstrap | Bicep, the preflight, CI | target tenant |
| `location` | Azure region name, passing the preflight's region checks | `eastus2` / `eastus2` / `eastus2` | Bicep, the preflight | region of every regional resource; moving it means a new resource group |
| `staticWebAppLocation` | a Static Web Apps region | `eastus2` / `eastus2` / `eastus2` | Bicep, the preflight (check 2) | region of the Static Web App |
| `network.addressSpace` | IPv4 /22 | `10.61.0.0/22` / `10.62.0.0/22` / `10.63.0.0/22` | Bicep | the environment network; subnets derived with `cidrSubnet()` |
| `webCustomDomains` | array of host names; at most 2 when `web.sku` is `Free`, 5 when `Standard` (preflight check 14, `custom_domains_exceed_plan`) | `[]` / `[]` / `[]` | Bicep | the web registration's redirect URIs, the API's platform CORS origins and the Static Web Apps custom domains; never the CSP |
| `web.sku` | `Free`, `Standard` | `Free` / `Standard` / `Free` | Bicep | Static Web Apps plan |
| `api.sku` | `B1`, `P0v3`, `P1v3`, `P2v3` | `B1` / `P1v3` / `P1v3` | Bicep; derives `DOCCHAT_API_WORKERS` | API plan |
| `api.minInstances` | integer 1 to 30 | 1 / 2 / 1 | Bicep (autoscale) | autoscale minimum |
| `api.maxInstances` | integer 1 to 30 | 1 / 6 / 6 | Bicep (autoscale) | autoscale maximum |
| `api.zoneRedundant` | bool; `true` needs a Premium v3 `api.sku` and raises the plan's instances and every autoscale minimum to at least 2 | `false` / `false` / `false` | Bicep (the API plan); in `dev`, `true` also makes preflight check 8 run | zone redundancy of the API plan; it switches on in place only while the plan reports `maximumNumberOfZones` above 1, otherwise only by redeployment into a new resource group (runbook "Zone-redundancy conversions") |
| `ingestion.sku` | `EP1`, `EP2`, `EP3` | `EP1` / `EP2` / `EP2` | Bicep; derives the three ingestion maps of section 4.5 | ingestion plan |
| `ingestion.alwaysReady` | integer 1 to 20 | 1 / 1 / 2 | Bicep | always-ready instances |
| `ingestion.maxBurst` | integer 1 to 100, at most the region's `linuxPremiumMaxInstances` in `infra/config/regions.json` (preflight check 4) | 2 / 4 / 4 | Bicep, the preflight | plan and app scale limit |
| `sql.sku` | `GP_S_Gen5_2`, `GP_Gen5_2`, `GP_Gen5_4` | `GP_S_Gen5_2` / `GP_Gen5_2` / `GP_Gen5_2` | Bicep | database SKU |
| `sql.minCapacity` | number 0.5 to 2 vCores, serverless only | 0.5 / not used / not used | Bicep | serverless floor |
| `sql.autoPauseDelayMinutes` | -1, or 15 to 10,080 | 15 / -1 / -1 | Bicep | `dev` auto-pause after 15 minutes idle |
| `sql.maxSizeGb` | integer 1 to 1,024 GB | 32 / 64 / 32 | Bicep | database size |
| `sql.zoneRedundant` | bool | `false` / `false` / `false` | Bicep, read only in `prod` (the `dev` and `load` databases are always `zoneRedundant: false`); in `dev`, `true` only makes preflight check 8 run | zone redundancy of the `prod` database (runbook "Zone-redundancy conversions") |
| `sql.backupRedundancy` | `Local`, `Zone`, `Geo` | `Local` / `Geo` / `Local` | Bicep | backup storage |
| `sql.pitrDays` | integer 1 to 35 days | 7 / 14 / 7 | Bicep | point-in-time retention |
| `cosmos.serverless` | bool | `true` / `false` / `false` | Bicep | capacity mode |
| `cosmos.backupTier` | `Continuous7Days`, `Continuous30Days` | `Continuous7Days` / `Continuous30Days` / `Continuous7Days` | Bicep | continuous backup tier |
| `cosmos.maxThroughput` | object `{conversations, memories, usage}`, each 1,000 to 100,000 RU/s | not used / `{conversations: 4000, memories: 1000, usage: 1000}` / `{conversations: 30000, memories: 30000, usage: 30000}` | Bicep | autoscale maxima, applied when the containers are created, so a changed value reaches only a container the provision creates; `load`'s are 60 turns per second × 400 RU per completed turn × 1.25 |
| `conversationRetentionDays` | integer 0 to 3,650 days, 0 = keep | 0 / 0 / 0 | Bicep | the `conversations` container's `defaultTtl`: `-1` for 0, otherwise the value × 86,400 seconds (`13-conversations-and-memory.md` section 9) |
| `memoryRetentionDays` | integer 0 to 3,650 days, 0 = keep | 0 / 0 / 0 | Bicep | the `memories` container's `defaultTtl`, by the same rule |
| `cosmosGroupDataAccess` | bool | `true` / `true` / `true` | Bicep; the azd output `DOCCHAT_COSMOS_GROUP_DATA_ACCESS`, read by `docchat-ops` | the operator groups' Cosmos DB data-plane assignments |
| `search.sku` | `basic`, `standard` | `basic` / `standard` / `standard` | Bicep; derives `DOCCHAT_SEARCH_MAX_INDEXES` | AI Search tier |
| `search.replicas` | integer 1 to 12; at least 2 outside `dev` (section 4.5) | 1 / 2 / 2 | Bicep | replicas |
| `search.partitions` | integer, exactly 1 | 1 / 1 / 1 | Bicep | partitions |
| `openAi.enabled` | bool; exactly one of `openAi.enabled` and `modelStub.enabled` is `true` | `true` / `true` / `false` | Bicep, the preflight (checks 6 and 7 run only while `true`) | declares the Azure OpenAI account and its deployments |
| `modelStub.enabled` | bool | `false` / `false` / `true` | Bicep | declares the model stub site and its private endpoint, and points `DOCCHAT_OPENAI_ENDPOINT` at it |
| `openAi.deployments` | object `{chat, chatSmall, tableSummary}`, each an `openAiDeployment` (section 4.3) | section 4.3 | Bicep, the preflight; the azd outputs `DOCCHAT_OPENAI_CHAT_CAPACITY` and `DOCCHAT_OPENAI_SMALL_CAPACITY` | the chat model deployments and their app settings |
| `openAi.embeddingModels` | array of 1 to 8 `embeddingModel` entries (section 4.4) | section 4.4 | Bicep, the preflight; the app setting `DOCCHAT_OPENAI_EMBEDDING_MODELS` | the embedding deployment pairs |
| `storage.malwareScanning` | bool | `false` / `true` / `false`; `dev` is `true` only while the deployed malware-scanning test runs: the stage-2 acceptance performer sets it with one pull request and restores `false` with a second as soon as the suite ends, pass or fail | Bicep (Defender for Storage on the data account); the app setting `DOCCHAT_STORAGE_MALWARE_SCANNING_ENABLED` | malware scanning of uploads |
| `storage.malwareScanCapGbPerMonth` | integer 1 to 10,000 GB | 1,000 / 1,000 / 1,000 | Bicep | Defender scan cap per month |
| `keyVault.purgeProtection` | bool; once `true` it can't be turned off | `false` / `true` / `false` | Bicep | purge protection of the environment vault |
| `keyVault.softDeleteDays` | integer 7 to 90 days | 7 / 90 / 7 | Bicep | soft-delete retention |
| `monitoring.retentionDays` | integer 30 to 730 days | 30 / 90 / 30 | Bicep | workspace retention |
| `monitoring.dailyCapGb` | -1 (no cap), or 1 to 100 GB | 2 / -1 / -1 | Bicep | workspace daily cap; `load` has none because a full load run sends about 6.5 GB, and every load verdict checks `_LogOperation` for `OverQuota` |
| `telemetrySamplingRatio` | number 0.1 to 1.0 | 1.0 / 1.0 / 1.0 | Bicep; the app setting `DOCCHAT_TELEMETRY_SAMPLING_RATIO` | trace sampling ratio |
| `budget.enabled` | bool | `true` / `true` / `true` | Bicep | monthly budget declared or not |
| `budget.amount` | integer, USD per month | 650 / 2,200 / 400 | Bicep | budget amount (runbook "Budget alert at 80% or 100%") |
| `groups` | object of object IDs `{operators, sqlAdmins, users, admins, configOperators, conversationReaders, userDataOperators}` | written by the bootstrap | Bicep (role assignments, the SQL administrator); `sql_principals.py` (contained users of `configOperators`, `conversationReaders`, `userDataOperators`); `tighten_conditions.py` (`operators`, `configOperators`) | the environment's Entra groups |
| `alerts.enabled` | bool | `true` / `true` / `false` | Bicep (`infra/modules/monitoring/alerts.bicep`) | alert rules deployed or not |
| `alerts.ttftP95Ms` | integer 2,000 to 120,000 ms | 20,000 / 20,000 / 20,000 | `alerts.bicep` | threshold of the alert `ttft-p95` |
| `alerts.openAiFirstChunkP95Ms` | integer 1,000 to 120,000 ms | 10,000 / 10,000 / 10,000 | `alerts.bicep` | threshold of the alert `openai-first-chunk-latency`: p95 from the answer call's start to its first chunk, per deployment |
| `alerts.openAiCallMeanMs` | integer 1,000 to 120,000 ms | 8,000 / 8,000 / 8,000 | `alerts.bicep` | threshold of the alert `openai-call-latency`: mean duration of successful model calls other than the streamed answer, per deployment and call |

### 4.3 Azure OpenAI deployments (`openAiDeployment`)

**Owner decision: the deployment type is a Bicep parameter of each deployment, and every deployment defaults to `GlobalStandard`** (ADR 0099). No deployment inherits another's type and Bicep derives none: the three chat model deployments each carry a `type` field, and each embedding model entry carries `queryType` and `ingestionType` (section 4.4), five type parameters in all. `GlobalStandard` processes prompts and completions in whichever Azure region has capacity, while the account, its deployments and every stored conversation stay in the environment's region; the product owner has no data-residency requirement. The committed environment files state every type explicitly, each `GlobalStandard`, so the default is visible in review.

`openAiDeployment` (sealed, `infra/types.bicep`; `83-infrastructure.md` section 5.2):

| Field | Type and rule |
|---|---|
| `name` | string, 2 to 64 characters of letters, digits, `-`, `_` and `.` |
| `model` | string, the model name |
| `version` | string, the pinned model version, never omitted |
| `type` | `'GlobalStandard'` (default) \| `'DataZoneStandard'` \| `'Standard'`, written to `sku.name` |
| `capacity` | integer 1 to 10,000 capacity units (1,000 tokens per minute each), written to `sku.capacity` |
| `versionUpgradeOption` | `'NoAutoUpgrade'` \| `'OnceCurrentVersionExpired'` \| `'OnceNewDefaultVersionAvailable'` |
| `raiPolicyName` | string, the content filter policy |
| `encoding` | `'o200k_base'` \| `'cl100k_base'`, surfaced as an app setting |

Defaults of `openAi.deployments`:

| Key | `name` | `model` | `version` | `type` | `capacity` `dev` / `prod` | `versionUpgradeOption` | `raiPolicyName` | `encoding` | App settings it feeds |
|---|---|---|---|---|---|---|---|---|---|
| `chat` | `chat` | `gpt-5.6-sol` | `2026-07-09` | `GlobalStandard` | 30 / 500 | `OnceCurrentVersionExpired` | `docchat-text-guard` | `o200k_base` | `DOCCHAT_OPENAI_CHAT_DEPLOYMENT`, `DOCCHAT_OPENAI_CHAT_MODEL`, `DOCCHAT_OPENAI_CHAT_MODEL_VERSION`, `DOCCHAT_OPENAI_CHAT_ENCODING` |
| `chatSmall` | `chat-small` | `gpt-5.6-terra` | `2026-07-09` | `GlobalStandard` | 30 / 530 | `OnceCurrentVersionExpired` | `docchat-text-guard` | `o200k_base` | `DOCCHAT_OPENAI_SMALL_DEPLOYMENT`, `DOCCHAT_OPENAI_SMALL_MODEL`, `DOCCHAT_OPENAI_SMALL_MODEL_VERSION`, `DOCCHAT_OPENAI_SMALL_ENCODING` |
| `tableSummary` | `ingest-summary` | `gpt-5.6-terra` | `2026-07-09` | `GlobalStandard` | 30 / 150 | `OnceCurrentVersionExpired` | `docchat-table-summary-guard` | `o200k_base` | `DOCCHAT_OPENAI_TABLE_SUMMARY_DEPLOYMENT`, `DOCCHAT_OPENAI_TABLE_SUMMARY_ENCODING` |

**`load`.** `load` has no Azure OpenAI account. `infra/env/load.json` carries the same `openAi.deployments` and `openAi.embeddingModels` objects as `infra/env/prod.json`, because the sealed type requires them and the apps' deployment-name, model, version and encoding settings come from them; with `openAi.enabled` `false`, Bicep reads only those four fields, declares no deployment and outputs empty capacities, and the preflight skips checks 6 and 7.

**Rules.**

1. A change of type, version or capacity is a pull request to `infra/env/<env>.json`. The pull-request job runs the preflight against `dev`, and the provision of the target environment runs it first.
2. Preflight check 6 checks each deployment's pinned model and version against that deployment's own type (`model_version_not_offered`, `model_retired`, `model_deprecated`, `model_sku_not_offered`, and `model_retires_soon` 183 days before retirement); check 7 checks the quota of that type in the region (`quota_insufficient`, with the shortfall in units). Either failure stops the provision before any resource changes.
3. In `prod`, raising `chat` or `chatSmall` capacity is the remedy when the capacity invariant fails (section 3.6); the `postprovision` hook re-runs the check with the new capacity outputs.
4. The apps address deployments by name only, so a type change needs no code or app-setting change.
5. A version change follows the runbook "Model version upgrade" (`86-operations.md`), including the capability-table row and the `pricing.model_rates` entry the new model needs.

### 4.4 Embedding models (`embeddingModel`)

Each entry of `openAi.embeddingModels` (sealed type `embeddingModel`) produces a deployment pair, `embed-query-<key>` (called only by the API) and `embed-ingest-<key>` (called only by the ingestion app), both `versionUpgradeOption: 'NoAutoUpgrade'` and with no `raiPolicyName`, because content filtering doesn't apply to embedding models.

| Field | Type and rule | Default entry (`dev` / `prod`) |
|---|---|---|
| `key` | string, unique in the array; part of both deployment names | `te3-large-v1` |
| `model` | string | `text-embedding-3-large` |
| `version` | string, pinned | `1` |
| `queryType` | `'GlobalStandard'` (default) \| `'DataZoneStandard'` \| `'Standard'`, the type of `embed-query-<key>` | `GlobalStandard` |
| `ingestionType` | the same values, the type of `embed-ingest-<key>` | `GlobalStandard` |
| `queryCapacity` | integer 1 to 10,000 units | 30 / 100 |
| `ingestionCapacity` | integer 1 to 10,000 units | 60 / 250 |
| `defaultDimensions` | integer, one of `allowedDimensions` | 1024 |
| `allowedDimensions` | array of integers | `[256, 1024, 3072]` |
| `maxInputTokens` | integer | 8192 |
| `encoding` | `'o200k_base'` \| `'cl100k_base'` | `cl100k_base` |

A new embedding model or version is a new entry with a new key, so a new deployment pair; an entry is removed only when no physical index names its deployments, because an index keeps the names it copied (`14-ingestion.md` section 8.2). Changing an entry's type keeps its model and version, so every index that names the deployment stays valid. Both apps receive the array as `DOCCHAT_OPENAI_EMBEDDING_MODELS` (section 2.3).

### 4.5 Values derived in `main.bicep`

Derived from `environmentName` or a SKU, never parameters, so no environment file can change them (ADR 0102):

| Value | Rule |
|---|---|
| Storage SKU of both accounts | `Standard_LRS` in `dev`; `Standard_ZRS` in `prod` and `load` |
| Cosmos DB `isZoneRedundant` | `false` in `dev`; `true` in `prod` and `load` |
| AI Search replicas | at least 2 outside `dev`: `main.bicep` fails the build with `fail('search.replicas must be at least 2 in prod and load')` when `prod` or `load` asks for fewer |
| `DOCCHAT_API_WORKERS` | `{B1: 1, P0v3: 1, P1v3: 2, P2v3: 4}[api.sku]` |
| `FUNCTIONS_WORKER_PROCESS_COUNT` | `{EP1: 1, EP2: 2, EP3: 4}[ingestion.sku]` |
| `maxConcurrentActivityFunctions` | `{EP1: 2, EP2: 4, EP3: 8}[ingestion.sku]`, two activity slots per worker process |
| `maxConcurrentOrchestratorFunctions` | `{EP1: 2, EP2: 4, EP3: 8}[ingestion.sku]` |
| `DOCCHAT_SEARCH_MAX_INDEXES` | `{basic: 15, standard: 50}[search.sku]` |
| `resourceToken` | `83-infrastructure.md` section 1.4, salted with `resourceTokenSalt` |

The only zone parameters are `api.zoneRedundant` and `sql.zoneRedundant`. `infra/config/regions.json` holds the region facts no GA API answers (`linuxPremiumMaxInstances`, `promptShieldsRegions`, `semanticRankerRegions`, `documentIntelligenceLayoutRegions`, `githubPrivateNetworkingRegions`), read by the preflight, by `main.bicep` and by the bootstrap's `ci-runner` step, each changed by a reviewed pull request (`83-infrastructure.md` section 3.5).

### 4.6 Shared parameters (`infra/shared.bicepparam`)

Deployed only by the bootstrap's `shared` step into `rg-docchat-shared`. None is secret.

| Parameter | Source | Type | Default | Allowed | Changed by | Read by | Purpose |
|---|---|---|---|---|---|---|---|
| `sharedOperatorIpAllowlist` | `DOCCHAT_SHARED_OPERATOR_IP_ALLOWLIST` from the operator's shell, never a GitHub variable | string array | empty | at most 20 public IPv4 addresses, comma-separated | operators, then a re-run of the `shared` step | `infra/shared.bicep` | the IP rules of the shared registry and the shared test vault; while it is empty the test vault keeps `publicNetworkAccess: 'Enabled'` with `defaultAction: 'Deny'` and no rule by default, so only a test job's transient runner rule reaches it, and gets `publicNetworkAccess: 'Disabled'` under the private-networking option (ADR 0122); no CI job receives it, so leftover runner rules on the registry and the vault are removed only through the runbook "Remove a stray runner rule" |
| `sharedRegistryLocation` | constant in the file | region | `eastus2` | an Azure region | pull request | `infra/shared.bicep` | the registry's region |
| `ciPrivateNetworking` | `DOCCHAT_CI_PRIVATE_NETWORKING`, which the `shared` step sets from `bootstrap.config.json` | bool | `false` | `true` or `false` | the bootstrap operator (config file, section 4.7) | `infra/shared.bicep` | `false`: standard GitHub-hosted test runners reach the test vault through a transient runner rule, and nothing below the vault is declared; `true` (private-networking option): declares the CI network, its network security groups, the vault's private endpoint and its zone, and gives the vault `publicNetworkAccess: 'Disabled'` while the shared allowlist is empty (`83-infrastructure.md` sections 2.17 and 4.2; ADR 0122) |
| `ciNetworkLocation` | `DOCCHAT_CI_NETWORK_LOCATION`, which the `shared` step sets from `bootstrap.config.json` | region | `eastus2` | one of `githubPrivateNetworkingRegions` in `infra/config/regions.json`; under the private-networking option any other value stops the `ci-runner` step with `ci_region_unsupported` | the bootstrap operator (config file) | `infra/shared.bicep`, only when `ciPrivateNetworking` is `true` | the region of `vnet-docchat-ci` and of the larger runners of the private-networking option; unused by default |
| `securityWorkspaceId` | written by the bootstrap | resource ID | the bootstrap value | the resource ID of `log-docchat-security` | bootstrap | `infra/shared.bicep` | diagnostic settings of the shared resources |
| `githubOrganizationDatabaseId` | parameter of `infra/bootstrap/ci-network-settings.bicep`, read by the bootstrap's `ci-runner` step from the GitHub organization | integer | the organization's `databaseId` | that value | bootstrap | `GitHub.Network/networkSettings@2024-04-02` `ghns-docchat-ci` (`businessId`), deployed only while it doesn't exist | the CI runner group's network settings (private-networking option only) |

### 4.7 Bootstrap configuration (`scripts/bootstrap/bootstrap.config.json`)

Read by `scripts/bootstrap/bootstrap.py` and never by `main.bicep`; committed, holding no IP address and no personal email address; changed by the bootstrap operator through a reviewed pull request, effective at the next run of the bootstrap step that reads it (`--step <name>`). The keys are those of `83-infrastructure.md` section 8.5: `tenantId`, `subscriptions`, `location`, `github.organization`, `github.repository`, `ciPrivateNetworking`, `ciNetworkLocation`, `operators`, `approverObjectIds`, `securityAlertEmails`, `testUserAliases`, `allowedRoleAssigneePrincipalIds`, `templateCosmosAssignmentIds` and `groupOwners`. The ones with rules of their own:

| Key | Type | Default | Allowed | Changed by | Read by | Purpose |
|---|---|---|---|---|---|---|
| `ciPrivateNetworking` | bool | `false` | `false` (standard GitHub-hosted test runners and the test vault's transient runner rule, on every GitHub plan) or `true` (larger runners joined to the CI network, for an organization on GitHub Team or GitHub Enterprise Cloud; a plan without larger runners stops the `ci-runner` step with `ci_private_networking_unavailable`) | the bootstrap operator, by a reviewed pull request followed by the bootstrap steps `shared`, `assignments`, `github` and `ci-runner` | the bootstrap: `providers`, `shared` (as `DOCCHAT_CI_PRIVATE_NETWORKING`, section 4.6), `assignments`, `github` (the repository variable `DOCCHAT_CI_PRIVATE_NETWORKING`, section 4.8) and `ci-runner` | the CI path to the test vault (`83-infrastructure.md` sections 8.5 and 10.16; ADR 0122) |
| `approverObjectIds` | UUID array per environment | none | at least 2 named people per environment, none of them in `operators`; otherwise the bootstrap stops with `approver_is_operator` | the bootstrap operator | the bootstrap | owners of the environment's just-in-time groups, the second person of every elevation (section 3.3); standing members of `sg-docchat-operators` for its reads |
| `operators` | UUID array | none | object IDs of people: the operators, and every maintainer, release approver, tenant administrator and Conditional Access Administrator who runs Azure reads | the bootstrap operator | the bootstrap | standing members of `sg-docchat-operators`; the people PIM for Groups makes eligible; the approver check |
| `groupOwners` | UUID array | none | at least 2 object IDs of people; otherwise the bootstrap's `prerequisites` step stops with `missing_prerequisite` before any change | the bootstrap operator | the bootstrap (`groups` step) | owners of every `sg-docchat-<env>-users` and `sg-docchat-<env>-admins` group, who manage their members |
| `securityAlertEmails` | string array | none | shared mailboxes or distribution lists, never a personal address | the bootstrap operator | `infra/bootstrap/security.bicep` | receivers of `ag-docchat-security`, to which every security alert of every environment goes |
| `allowedRoleAssigneePrincipalIds` | UUID array | seeded with `sg-docchat-operators`, `id-docchat-citest` and each `sg-docchat-<env>-config-operators` group | principal IDs | the bootstrap; extended by `tighten_conditions.py` with each environment's API and ingestion principals and, in `dev` and `prod` once it exists, the subscription's Defender `StorageDataScanner` principal (in `prod` a missing scanner stops the script with `principal_ids_missing`; in `dev` it is left out and the script is re-run (runbook "Re-tighten role-assignment conditions") before `dev` scanning is turned on) | `infra/bootstrap/security.bicep` (`docchat-alert-role-assignments`) | the principals a role assignment may name without raising the security alert |
| `templateCosmosAssignmentIds` | string array | empty until the first tightening | the template-declared Cosmos DB data-plane assignment resource IDs | written by `tighten_conditions.py` | `infra/bootstrap/security.bicep` (`docchat-alert-cosmos-data-access`) | the assignments the data-access alert excludes |
| `testUserAliases` | string array | `admin`, `member`, `outsider`, `norole`, `load-01` to `load-20`, `load-admin` | the aliases of `84-testing.md` section 6 | the bootstrap operator | the bootstrap; `docchat-ops testusers` | the non-production test users; each password is the secret `test-user-<alias>` in the shared test vault, rotated by the runbook "Test-user password rotation" |

### 4.8 GitHub environments and variables

The repository holds no Actions secret; every value below is a GitHub variable, not a secret. `scripts/bootstrap/configure_github.py` writes the environments `dev`, `prod`, `load`, `load-access`, `dev-tests` and `load-tests` (`83-infrastructure.md` section 10.4); `load-access` is a gate with no variable, and `dev-tests` and `load-tests` hold only the variables named for them below. A changed variable takes effect at the next workflow run that reads it.

| Name | Scope | Value | Set by | Read by |
|---|---|---|---|---|
| `AZURE_CLIENT_ID` | environments `dev`, `prod`, `load` | that environment's deploy identity's client ID | bootstrap | CI (OIDC sign-in) |
| `AZURE_TENANT_ID` | environments `dev`, `prod`, `load`, `dev-tests`, `load-tests`; repository | the tenant ID | bootstrap | CI |
| `AZURE_SUBSCRIPTION_ID` | environments `dev`, `prod`, `load`, `dev-tests`, `load-tests` | the subscription ID | bootstrap | CI |
| `AZURE_LOCATION` | environments `dev`, `prod`, `load`, `load-tests` | the region | bootstrap | CI |
| `AZURE_RESOURCE_GROUP` | environments `dev`, `prod`, `load` (`rg-docchat-<env>`); `load-tests` (`rg-docchat-load`) | `rg-docchat-<env>` | bootstrap | CI |
| `DOCCHAT_OPERATOR_IP_ALLOWLIST` | environments `dev`, `prod`, `load` | comma-separated public IPv4 addresses, at most 20 | operators (runbook "Change the operator allowlist") | CI, the Bicep parameter `operatorIpAllowlist` |
| `DOCCHAT_OPS_ALERT_EMAILS` | environments `dev`, `prod`, `load` | comma-separated shared mailboxes | operators | CI, the Bicep parameter `opsAlertEmails` |
| `DOCCHAT_CI_TEST_CLIENT_ID` | environments `dev-tests`, `load-tests` only, never `dev` or `load` | `id-docchat-citest`'s client ID | bootstrap | the deployed, load and seeding jobs and the nightly `test-credential-expiry` job (`testusers check`), each with the `runs-on` expression of `83-infrastructure.md` section 10.16: a standard `ubuntu-24.04` runner by default, the runner group `docchat-ci-private` under the private-networking option |
| `DOCCHAT_TEST_VAULT_URI` | environments `dev-tests`, `load-tests` only | `https://kv-docchat-tst-<sharedToken>.vault.azure.net/` | bootstrap | the same jobs (by default inside each job's read window, under the private-networking option through the vault's private endpoint); `firewall.py --target vault`; `docchat-ops testusers` from an address in the shared allowlist |
| `DOCCHAT_TEST_USER_DOMAIN` | environments `dev-tests`, `load-tests` only | the tenant's initial domain | bootstrap | the same jobs; `docchat-ops testusers` |
| `DOCCHAT_TEST_NOROLE_SKIP_REASON` | environment `dev-tests` only; not created by the bootstrap | unset, or `graph_default_access_refused` | a repository administrator (`docchat-maintainers`), only when `identity_post_provision.py` exits 2 at the no-role default-access assignment (runbook "First admins and test identities") | the deployed `smoke` suite in `dev`, which then records the `norole` check as skipped with this reason |
| `DOCCHAT_PR_CLIENT_ID` | repository | the pull-request identity `id-docchat-pr`'s client ID | bootstrap | the pull-request jobs |
| `DOCCHAT_DEV_SUBSCRIPTION_ID` | repository | `dev`'s subscription | bootstrap | the what-if job |
| `DOCCHAT_DEV_OPERATOR_IP_ALLOWLIST` | repository | `dev`'s allowlist | operators | the what-if job |
| `DOCCHAT_CI_PRIVATE_NETWORKING` | repository | `true` or `false`, from `ciPrivateNetworking` (section 4.7; default `false`) | bootstrap (`github` step) | the `runs-on` expression of every `dev-tests` and `load-tests` job (`83-infrastructure.md` section 10.16) |
| `DOCCHAT_SHARED_OPERATOR_IP_ALLOWLIST` | the operator's shell only, never GitHub | comma-separated public IPv4 addresses | operators | the bootstrap's `shared` step (section 4.6); `firewall.py sweep --target registry` and `--target vault` in the runbook "Remove a stray runner rule"; never a CI job |

### 4.9 The azd environment (`.azure/<env>/.env`)

Written by azd on the runner, or on the operator's machine (gitignored and refused by the forbidden-file check, section 2.11); never committed and never secret.

1. **Outputs of `main.bicep`** (`83-infrastructure.md` section 8.8): `AZURE_TENANT_ID`, `WEB_CLIENT_ID`, `API_CLIENT_ID`, `TEST_CLIENT_ID` (empty in `prod`), `API_SCOPE` (`api://<tenantId>/docchat-<env>-<token>-api/access_as_user`), `API_BASE_URL` (`https://<API default host name>`), `WEB_BASE_URL`, `DOCCHAT_RESOURCE_TOKEN`, the app names `DOCCHAT_STATIC_WEB_APP_NAME`, `DOCCHAT_API_APP_NAME`, `DOCCHAT_INGESTION_APP_NAME` and `DOCCHAT_MODEL_STUB_APP_NAME` (empty outside `load`), `DOCCHAT_SQL_SERVER_NAME`, `DOCCHAT_SQL_SERVER`, `DOCCHAT_SQL_DATABASE`, `DOCCHAT_COSMOS_ACCOUNT_NAME`, `DOCCHAT_COSMOS_ENDPOINT`, `DOCCHAT_COSMOS_DATABASE`, `DOCCHAT_COSMOS_GROUP_DATA_ACCESS`, `DOCCHAT_STORAGE_ACCOUNT_NAME`, `DOCCHAT_STORAGE_BLOB_ENDPOINT`, `DOCCHAT_STORAGE_QUEUE_ENDPOINT`, `DOCCHAT_SEARCH_SERVICE_NAME`, `DOCCHAT_SEARCH_ENDPOINT`, `DOCCHAT_OPENAI_ACCOUNT_NAME`, `DOCCHAT_OPENAI_ENDPOINT`, `DOCCHAT_OPENAI_CHAT_CAPACITY`, `DOCCHAT_OPENAI_SMALL_CAPACITY`, `DOCCHAT_CONTENTSAFETY_ENDPOINT` (which `docchat-ops chat model-check` reads for its Prompt Shields probes), `DOCCHAT_KEY_VAULT_NAME`, `DOCCHAT_KEY_VAULT_URI`, `DOCCHAT_API_IDENTITY_NAME`, `DOCCHAT_API_IDENTITY_CLIENT_ID`, `DOCCHAT_API_IDENTITY_PRINCIPAL_ID`, `DOCCHAT_INGESTION_IDENTITY_NAME`, `DOCCHAT_INGESTION_IDENTITY_CLIENT_ID`, `DOCCHAT_INGESTION_IDENTITY_PRINCIPAL_ID` (the two workload identities' resource name, client ID and principal object ID, which the grants step and the operator commands read), `DOCCHAT_LOG_ANALYTICS_WORKSPACE_ID`, `DOCCHAT_LOG_ANALYTICS_RESOURCE_ID`, `DOCCHAT_APPINSIGHTS_NAME`, `DOCCHAT_OPERATORS_GROUP_ID`, `DOCCHAT_CONFIG_OPERATORS_GROUP_ID`, `DOCCHAT_COSMOS_ROLE_ASSIGNMENT_IDS`, and `DOCCHAT_SEARCH_MAX_INDEXES` and `DOCCHAT_OPENAI_EMBEDDING_MODELS` (each the app setting's value from the same source expression, sections 2.3 and 2.4, which `docchat-ops reindex` and `ingestion` read for the index-capacity check and the embedding catalogue).
2. **Values set before a provision:** `AZURE_ENV_NAME`, `AZURE_SUBSCRIPTION_ID` and `AZURE_LOCATION` (`azd env new`); `AZURE_RESOURCE_GROUP`, `DOCCHAT_API_IMAGE`, `DOCCHAT_INGESTION_IMAGE`, `DOCCHAT_MODEL_STUB_IMAGE` (the release's digest references), `DOCCHAT_OPERATOR_IP_ALLOWLIST`, `DOCCHAT_OPS_ALERT_EMAILS`, `DOCCHAT_RELEASE_SHA` and `DOCCHAT_RESOURCE_TOKEN_SALT` (`azd env set` by the deploy job from the GitHub variables and the release); `DOCCHAT_PREFLIGHT_DONE` (`true` after the job's own preflight step, so the `preprovision` hook doesn't run it twice); and, written by the `preprovision` hook, `DOCCHAT_API_IMAGE_CURRENT`, `DOCCHAT_INGESTION_IMAGE_CURRENT` and `DOCCHAT_MODEL_STUB_IMAGE_CURRENT` (each site's live image, or the release's when the site doesn't exist yet) and `DOCCHAT_BUDGET_START_DATE`.

| Value | Type | `dev` / `prod` / `load` | Allowed | Read by | Purpose |
|---|---|---|---|---|---|
| `DOCCHAT_OPENAI_CHAT_CAPACITY` | integer, capacity units, as decimal text | 30 / 500 / empty | 1 to 10,000; empty counts as absent | `docchat-ops chat capacity-check` only; never an app setting | the chat share of the capacity invariant (section 3.6) |
| `DOCCHAT_OPENAI_SMALL_CAPACITY` | integer, capacity units, as decimal text | 30 / 530 / empty | as above | `docchat-ops chat capacity-check` only | the chat-small share |

The web generator reads `AZURE_TENANT_ID`, `WEB_CLIENT_ID`, `API_BASE_URL` and `API_SCOPE` from this file and nothing else (section 5.2); the operator commands of `docchat-ops` read endpoints and names from it, so no operator types an endpoint.

### 4.10 Platform authentication and other values set only in Bicep

The API site's platform authentication is `Microsoft.Web/sites/config@2025-03-01` named `authsettingsV2`, declared in Bicep and nowhere else; a change is a reviewed pull request, effective at the next provision.

| Property | Value |
|---|---|
| `platform.enabled`, `platform.runtimeVersion` | `true`, `'~1'`; `platform.configFilePath` not set |
| `globalValidation` | `requireAuthentication: true`, `unauthenticatedClientAction: 'Return401'`, `excludedPaths: []`; `redirectToProvider` not set |
| `identityProviders.azureActiveDirectory.registration` | `openIdIssuer: 'https://login.microsoftonline.com/<tenantId>/v2.0'`, `clientId` = the API's `appId`; `clientSecretSettingName`, `clientSecretCertificateThumbprint`, `clientSecretCertificateIssuer` and `clientSecretCertificateSubjectAlternativeName` not set (bearer-only) |
| `identityProviders.azureActiveDirectory.login` | `disableWWWAuthenticate: false`, `loginParameters: []` |
| `identityProviders.azureActiveDirectory.validation` | `allowedAudiences: ['<API appId>']`; `defaultAuthorizationPolicy.allowedApplications: ['<web appId>']` in `prod`, `['<web appId>', '<test appId>']` in `dev` and `load`; `allowedPrincipals` and `jwtClaimChecks` not set |
| Other identity providers | `apple`, `facebook`, `gitHub`, `google`, `twitter`, `legacyMicrosoftAccount` and `customOpenIdConnectProviders` not declared; `identityProviders.azureActiveDirectory.isAutoProvisioned` never read or written |
| `login` | `tokenStore.enabled: false`, `preserveUrlFragmentsForLogins: false`, `allowedExternalRedirectUrls: []`; `routes`, `cookieExpiration`, `nonce` not set |
| `httpSettings` | `requireHttps: true`, `routes.apiPrefix: '/.auth'`, `forwardProxy.convention: 'NoProxy'` |

Other Bicep-only values with a configuration role:

| Value | Setting | Specified in |
|---|---|---|
| API platform CORS | `siteConfig.cors.allowedOrigins` = the Static Web App's default origin and each `webCustomDomains` origin; `supportCredentials: false` | `11-api.md` section 1.10 |
| API health check | `siteConfig.healthCheckPath: '/healthz'`, with `WEBSITE_HEALTHCHECK_MAXPINGFAILURES` 3 | `11-api.md` section 3.1.1 |
| App registrations | the web, API and test registrations and their redirect URIs, built from `webCustomDomains` and the Static Web App's default host name | `83-infrastructure.md` section 7 |
| Content filter policies | `docchat-text-guard`, `docchat-table-summary-guard` in `infra/modules/ai/openai-deployments.bicep` | `83-infrastructure.md` section 5.6 |
| Alert rules and thresholds | `infra/modules/monitoring/alerts.bicep`, fed by the `alerts.*` fields | `85-observability.md` |

### 4.11 Validation of delivery inputs

1. **Types.** `environmentConfig`, `openAiDeployment` and `embeddingModel` are sealed, so a missing, extra or mistyped field in any environment file fails `bicep build` and `bicep build-params` in the infrastructure gate of every pull request, whichever environment it targets.
2. **Preflight.** `scripts/preflight/preflight.py --env <env>` runs before every provision and in the pull-request what-if job against `dev`, reads `infra/env/<env>.json` and `infra/config/regions.json`, and exits 2 when any of its 14 checks fails, so nothing is provisioned (`83-infrastructure.md` section 3.3). The checks that judge configuration values: 4 (`ingestion.maxBurst` within the region's Linux maximum, `linux_premium_unavailable`), 6 (each deployment's model, version and type), 7 (quota per deployment type), 8 (availability zones, `availability_zones_unavailable`), 9 (the allowlist, `allowlist_invalid`), 12 (`prod`'s alert receivers, `alert_receivers_missing`) and 14 (`webCustomDomains` within the Static Web Apps plan, `custom_domains_exceed_plan`).
3. **Hooks.** The `preprovision` hook refuses an `AZURE_ENV_NAME` outside `dev`, `prod` and `load` (`env_name_invalid`), an `AZURE_RESOURCE_GROUP` other than `rg-docchat-<env>` (`resource_group_mismatch`) and any image reference that isn't a digest in the shared registry (`image_reference_invalid`); each exits 2 before anything is provisioned.
4. **After the provision.** The deployed app-settings comparison test (section 2.1) and the capacity check (section 3.6) run on every deploy.
5. **`infra/config/app-settings.json`** is checked by its unit test (section 2.2).

### 4.12 Load generator variables

The load workflow starts one Azure Container Instances group per scenario (`84-testing.md` sections 7.3 to 7.5) with these environment variables and nothing else. None is secret: the test users' tokens reach the container only as the file `tokens.json` on its read-only secret volume `work`.

| Variable | Value | Set by | Read by | Purpose and failure behaviour |
|---|---|---|---|---|
| `K6_URL` | the `url` of `tests/load/k6.lock.json` (the k6 2.3.0 release tarball) | `load-test.yaml`, from the lock | `tests/load/aci/run.py` | download source of k6; a download that fails 3 times prints `K6_BOOTSTRAP_FAILED download_failed`, which fails the scenario's verdict |
| `K6_SHA256` | the lock's `sha256` | `load-test.yaml`, from the lock | `run.py` | the tarball is refused unless its SHA-256 equals it (`K6_BOOTSTRAP_FAILED checksum_mismatch`) |
| `K6_MEMBER` | the lock's `member` (`k6-v2.3.0-linux-amd64/k6`) | `load-test.yaml`, from the lock | `run.py` | the one archive member written to `/tmp/k6`; a tarball without it prints `K6_BOOTSTRAP_FAILED member_missing` |
| `DOCCHAT_LOAD_TOKENS_FILE` | `/work/tokens.json` | the container group definition | `scenario.js` and `lib.js` | the test users' access tokens; a missing or unreadable file fails k6's setup, so the run prints a non-zero `K6_EXIT` |
| `DOCCHAT_LOAD_ANSWER_TOKENS` | L, integer 80 to 4,000 tokens (default 80), the workflow input `answer_tokens` | the S3 jobs | `scenario.js` and `lib.js` | appended to every question as `[[stub:tokens=<L>]]` |
| `DOCCHAT_LOAD_TOKEN_DELAY_MS` | d = max(1, floor(800 ÷ L)) ms | the S3 jobs | `scenario.js` and `lib.js` | appended as `[[stub:delay_ms=<d>]]`, so each answer streams for about 800 ms |
| `DOCCHAT_LOAD_RATE` | R turns per second, 5 to 60, written by `docchat-load s3-rate` | job `s3-scaleout` | `scenario.js` | the main phase's constant arrival rate; R below 5 ends S3 `inconclusive` before the main phase starts |

The workflow passes no other variable to a container group. A scenario that runs with one of its variables missing reads it as empty, fails its k6 thresholds and writes a `failed` verdict.

## 5. Web client configuration

The web client's configuration is public by design: the bundle and its configuration file carry only the four values below, and one build serves every environment (ADR 0086). `10-web-client.md` section 2 specifies loading, the generator and browser storage.

### 5.1 `config.json`

A JSON object with exactly these four members, in this order, and nothing else; written per environment at deploy time; fetched at start-up with `fetch('/config.json', { cache: 'no-store', credentials: 'same-origin', redirect: 'error' })` and parsed with a strict Zod object that rejects unknown members. None is secret. A changed value takes effect at the next web deploy of the environment and in each browser at its next page load.

| Member | Source (azd value) | Type | Default | Allowed | Changed by | Read by | Purpose |
|---|---|---|---|---|---|---|---|
| `tenantId` | `AZURE_TENANT_ID` | UUID | none (required) | canonical lower-case UUID | the deploy pipeline | the client at start-up | MSAL authority |
| `clientId` | `WEB_CLIENT_ID` | UUID | none (required) | the environment's web registration's application ID, lower-case | the deploy pipeline | the client at start-up | MSAL client |
| `apiBaseUrl` | `API_BASE_URL` | URL origin | none (required); in Azure always the API's default App Service host, because the API has no custom domain in the first version | `https://` scheme, lower-case host, optional port, no path, query, fragment or trailing slash; `http://localhost:<port>` or `http://127.0.0.1:<port>` only when the page itself is served from that host | the deploy pipeline | the client (API calls); the generator (the CSP's `connect-src`) | the API's origin |
| `apiScope` | `API_SCOPE` | string | none (required) | an `api://` URI ending in `/access_as_user`, at most 255 characters | the deploy pipeline | the client | the only scope the client requests |

**Failure.** A network error, a status other than `200`, invalid JSON or any rule failure shows the Configuration error screen, whose Reload button reloads the page (`10-web-client.md` section 4.8); the client never falls back to a default value. Nothing else configures the client: `import.meta.env` is read only for `DEV`, and there are no build-time environment values, feature flags or endpoints in code.

### 5.2 The generator and the deployment token

1. `apps/web/scripts/write-env-config.mjs` (Node 24.21.0, no dependencies) is the only producer of `config.json` and `staticwebapp.config.json`. Its inputs are exactly `AZURE_TENANT_ID`, `WEB_CLIENT_ID`, `API_BASE_URL` and `API_SCOPE`, taken from `azd env get-values` by `scripts/deploy/deploy_web.py`; its arguments are `--dist <dir>` (default `dist`) and `--allow-loopback`, which only the browser-test harness passes.
2. It validates each input by the rules of section 5.1. On any violation it writes nothing, prints one line naming the failing variable to standard error and exits with code 1, so the deploy job stops before upload.
3. It writes `<dist>/config.json` (compact JSON, UTF-8, one trailing newline) and `<dist>/staticwebapp.config.json`, the text of `staticwebapp.config.template.json` with its single placeholder `__API_ORIGIN__` replaced by `new URL(API_BASE_URL).origin`; a result above 20,480 bytes, Static Web Apps' limit, exits 1 and writes nothing.
4. The build output holds neither file: `scripts/check-dist.mjs` fails the build when `dist/` contains a `config.json` or `staticwebapp.config.json`, a source map, or an endpoint or key pattern (`10-web-client.md` section 1.7).
5. **The Static Web Apps deployment token** is the one key-based credential in delivery. `deploy_web.py` reads it at deploy time with `az staticwebapp secrets list --name <static web app> --resource-group rg-docchat-<env> --query properties.apiKey -o tsv`, masks it at once and passes it only in the child process's environment as `SWA_CLI_DEPLOYMENT_TOKEN` to the pinned `@azure/static-web-apps-cli` 2.0.10. It is never stored as a GitHub secret, written to a file or logged. The runbook "Static Web Apps deployment token reset" resets it every 90 days and whenever exposure is suspected.
6. After the upload, the deploy step checks that `https://<Static Web App host>/config.json` answers `200` with the expected `apiBaseUrl` and that the response's `Content-Security-Policy` names the API origin; a mismatch fails the job.

### 5.3 Browser storage

Per-viewer preferences, written and read only through `src/platform/storage.ts`, each access in `try`/`catch`, so a failure means the default. None holds personal data or user content.

| Key | Storage | Type | Default | Allowed | Changed by | Purpose |
|---|---|---|---|---|---|---|
| `docchat.theme` | `localStorage` | enum | absent: follows `prefers-color-scheme` | `light`, `dark` | the user (theme toggle) | theme |
| `docchat.nav.open` | `localStorage` | bool | absent: open from 1,280 px wide | `true`, `false` | the user (navigation toggle) | navigation drawer state |
| `docchat.signedOut` | `sessionStorage` | flag | absent | `1` present, or absent | the client on sign-out; removed on Sign in or a successful redirect result | shows the Signed out state instead of starting a sign-in |

### 5.4 Development values

`npm run dev` serves `/config.json` from `apps/web/config.local.json` (gitignored; its `apiBaseUrl` is `http://localhost:5173`) through a dev-only plugin, and proxies `/v1` and `/healthz` to `DOCCHAT_DEV_API_TARGET` (section 2.11), so the page calls a local API or the deployed `dev` API through its own origin and no environment needs a localhost CORS rule. `apps/web/config.example.json` holds the four members with placeholder values (`00000000-0000-0000-0000-000000000000` for both identifiers, `http://localhost:5173`, `api://00000000-0000-0000-0000-000000000000/docchat-api/access_as_user`).

### 5.5 Web-related values held elsewhere

`web.sku` and `webCustomDomains` (section 4.2); the API's platform CORS origins and the web registration's SPA redirect URIs, both built from the Static Web App's default host name and `webCustomDomains` (section 4.10); the deployment token (section 5.2). The client's own constants (limits, timeouts, polling intervals, breakpoints) are code, not configuration (`10-web-client.md` section 1.10).

## 6. Rules

### 6.1 Start-up validation and refusal

Every process validates its whole configuration before it serves, and a failure stops it before any request, message or timer is handled. The failure record names the code and the setting, never the value.

| Process | Where the checks run | On failure |
|---|---|---|
| API | `apps/api/gunicorn.conf.py` loads every settings class of the API (`CoreSettings`, `AuthSettings`, `TelemetrySettings`, `SqlSettings`, `LocalSettings`, `UploadSettings`, `EmbeddingModelCatalog`, `ApiSettings`) and runs the checks below before Gunicorn forks a worker; each worker's lifespan then creates the clients and loads the ops-config snapshot (section 3.1) | the master exits with code 78 after one error record on standard error; a lifespan failure stops the worker, and Gunicorn stops with its worker-boot error. `GET /healthz` never answers, App Service restarts the container, and the deploy's post-deploy health check fails |
| Ingestion app | `docchat_ingestion.function_app` at import, in the order of `14-ingestion.md` section 4.11 | one `job_failed` record with the code; a first-party exception with a constant message, so the host loads no function and ingestion fails closed; `ingestion_worker_started` isn't emitted, and the post-deploy check reports `ingestion_start_failed` |
| Model stub | `StubSettings` at import | Uvicorn exits with a non-zero code before it binds port 8000 |
| Web client | `config.json` parsing (section 5.1) | the Configuration error screen |
| Generator | `write-env-config.mjs` (section 5.2) | exit 1, nothing written |

Checks, in this order:

| # | Rule | Code | Message |
|---|---|---|---|
| 1 | Every setting a settings class declares is present unless the class gives it a default, and parses as its type within its range (sections 2.3 to 2.6, 2.11) | settings validation (`pydantic.ValidationError`; the record names each failing setting) | none shown to users |
| 2 | `DOCCHAT_LOCAL_EMULATORS` is `true` while `WEBSITE_SITE_NAME` or `WEBSITE_INSTANCE_ID` is set (both are set by App Service and Functions) | `local_emulators_in_azure` | "Local emulator mode can't run in Azure." |
| 3 | `DOCCHAT_LOCAL_EMULATORS` is `false` and any of `DOCCHAT_SQL_SERVER`, `DOCCHAT_COSMOS_ENDPOINT`, `DOCCHAT_STORAGE_BLOB_ENDPOINT`, `DOCCHAT_STORAGE_QUEUE_ENDPOINT`, `DOCCHAT_SEARCH_ENDPOINT`, `DOCCHAT_OPENAI_ENDPOINT`, `DOCCHAT_CONTENTSAFETY_ENDPOINT` or `DOCCHAT_DOCINTEL_ENDPOINT` the process reads uses `http://` or names a loopback host (`localhost`, an address in 127.0.0.0/8, or `::1`) | `insecure_endpoint` | "A configured endpoint isn't a secure remote address." |
| 4 | `DOCCHAT_CORE_ENVIRONMENT` is `local` while `WEBSITE_SITE_NAME` or `WEBSITE_INSTANCE_ID` is set | settings validation | none |
| 5 | `DOCCHAT_CORE_ENVIRONMENT` is `prod` and `DOCCHAT_AUTH_TEST_CLIENT_ID` is non-empty (API) | settings validation | none |
| 6 | `DOCCHAT_AUTH_API_CLIENT_ID`, `DOCCHAT_AUTH_WEB_CLIENT_ID` and a non-empty `DOCCHAT_AUTH_TEST_CLIENT_ID` aren't distinct (API) | settings validation | none |
| 7 | `DOCCHAT_CORE_IDENTITY_CLIENT_ID` is empty while `DOCCHAT_CORE_ENVIRONMENT` isn't `local` | settings validation | none |
| 8 | `DOCCHAT_OPENAI_CHAT_MODEL` or `DOCCHAT_OPENAI_SMALL_MODEL` names a model absent from `MODEL_CAPABILITIES`; a reasoning-effort setting holds a value outside its model's row; a prompt-cache setting is `disabled` for a model without `prompt_cache_options`; or an encoding can't load from `TIKTOKEN_CACHE_DIR` (API; `12-chat-pipeline.md` section 10.7) | settings validation | none |
| 9 | `DOCCHAT_TELEMETRY_LOG_LEVEL` is `DEBUG` while `DOCCHAT_LOCAL_EMULATORS` is `false` | settings validation | none |
| 10 | An entry of `DOCCHAT_OPENAI_EMBEDDING_MODELS` breaks section 2.3's shape (API, ingestion app) | settings validation | none |
| 11 | A registered ops-config area has a missing key or an invalid value (section 3.1) | settings validation (`ops_config_invalid` record naming the area and key) | none |
| 12 | `FUNCTIONS_WORKER_PROCESS_COUNT` (1 when unset) × `DOCCHAT_INGESTION_PARSE_SLOTS_PER_PROCESS` exceeds 8, half the 16 users of the parse-user pool because each slot holds two, or a pool lock file is missing (ingestion app; `14-ingestion.md` section 4.11 check 6) | `parse_pool_exceeded` | none |
| 13 | The isolation probe's child can't install its system-call filter, or any of its checks of the filter's effect fails: `socket`, `socketpair`, `io_uring_setup`, `ptrace(PTRACE_TRACEME)` or a self `process_vm_readv` not refused with `EPERM`, `clone3` not refused with `ENOSYS`, or a thread started after the load not filtered (ingestion app; `14-ingestion.md` sections 4.11 check 7 and 15.5) | `parse_isolation_failed` | none |

The start-up refusals `local_emulators_in_azure` and `insecure_endpoint` exit with code 78 in the API; `84-testing.md` names the unit tests of every row.

### 6.2 No secrets in settings

1. No app setting, image `ENV` value, environment file, GitHub variable, azd value, ops-config row or `config.json` member holds a key, password, client secret, SAS token or connection string with a key. Every Azure client authenticates with the process's one managed identity through `docchat_core.clients`, which accepts no key, connection string or SAS (ADR 0113).
2. `APPLICATIONINSIGHTS_CONNECTION_STRING` holds an ingestion endpoint and an instrumentation key, an identifier and not a credential once local authentication is off on the component; export authenticates through `APPLICATIONINSIGHTS_AUTHENTICATION_STRING` with Entra.
3. The secrets the design has, and where each lives: the test-user passwords (`test-user-<alias>` in the shared test vault, non-production only); `DOCCHAT_LOCAL_SQL_SA_PASSWORD` (a developer's gitignored `.env`, or a CI job's own environment, never Azure); the Static Web Apps deployment token (read at deploy time, section 5.2); and, only under content-share fallback 1, the host-account connection string, held in the environment vault and named by a Key Vault reference (section 2.8). The environment vault is empty otherwise, and no app has a Key Vault role.
4. The deployed app-settings comparison test fails on any setting outside this catalogue, so a secret can't be added by hand; no setting is set in the portal or by a script.
5. No settings value is logged or put on a span: start-up records name settings, the ops-config loader names areas and keys, and `config show` is the only reader that prints ops-config values.

### 6.3 Local-only settings, refused in Azure

| Setting | Guard |
|---|---|
| `DOCCHAT_LOCAL_EMULATORS` | `true` inside App Service or Functions refuses start-up with `local_emulators_in_azure` (section 6.1 check 2); it isn't in any site's set, so the app-settings comparison test fails if it appears |
| `DOCCHAT_CORE_ENVIRONMENT` = `local` | refused when `WEBSITE_SITE_NAME` or `WEBSITE_INSTANCE_ID` is set (check 4); Bicep writes only `dev`, `prod` or `load` |
| `DOCCHAT_LOCAL_SQL_SA_PASSWORD` | read only in local-emulator mode, which can't run in Azure; not in any site's set, so the comparison test fails if it appears |
| `DOCCHAT_DEV_API_TARGET` | read only by `vite.config.ts` in the dev server; the production bundle never reads it |
| `apps/web/config.local.json` | served only by the dev server; the build gate fails when `dist/` holds a `config.json` |
| `--allow-loopback` of the generator | passed only by the browser-test harness; the deploy script never passes it |
| `http://` and loopback endpoints | refused outside local-emulator mode (check 3) |

### 6.4 Outbound targets come only from settings

Every outbound base URL comes from an app setting validated at start-up by section 6.1's `insecure_endpoint` rule, or from the Entra key-set URL built from `DOCCHAT_AUTH_TENANT_ID`. No value read from a request, a SQL or Cosmos DB row, a blob, a document or a model output becomes the scheme, host or port of an outbound call (ADR 0150). The AST check `scripts/ci/check_outbound_clients.py` fails CI when an Azure client or credential is built outside `docchat_core.clients` or a transport timeout is missing.

### 6.5 One home per value

Each value has exactly one home, and every other place derives it: the sampling ratio only `telemetrySamplingRatio`; the Durable task hub name only its app setting, never `host.json`; deployment names, models, versions and encodings only `openAi.deployments` and `openAi.embeddingModels`; the chat capacities only those objects, reaching the capacity check as azd outputs; the web client's four values only the Bicep outputs; plan-dependent process counts only the SKU maps of section 4.5. A value needed in two layers is passed from its home by the pipeline, never typed twice.

### 6.6 Domain-agnostic configuration

No setting names a customer, an organisation, a subject area or a document collection, and no default assumes one. Product text, prompts and rules are code, versioned with the release; projects, indexes and their settings are data that administrators manage through the API.

## 7. Constants that are not settings

Values that shape behaviour but change only with a release, tested with the code. Making one configurable means adding it to this file first.

| Component | Where the constants are listed | Examples |
|---|---|---|
| API conventions | `11-api.md` sections 1.2 to 1.11 | clock skew 60 s; token length limit 16,384 characters; signing-key refresh every 3,600 s; ops-config refresh 60 s with a 15 s retry gap; JSON body limit 64 KiB; burst limit 40 tokens refilled at 10 per second |
| Chat pipeline | `12-chat-pipeline.md` section 18.6 | heartbeat interval 15 s; drain point 90 s after the stop signal; capacity share limit 25%; `MODEL_CAPABILITIES` (`12-chat-pipeline.md` section 10.7) |
| Conversations and memory | `13-conversations-and-memory.md` section 11.3 | title limit 80 code points; fact limit 200 code points; feedback comment limit 2,000 code points |
| Ingestion | `14-ingestion.md` section 18.3 | activity deadlines; `maxDequeueCount` 5 in `host.json`; the item lease margin |
| Web client | `10-web-client.md` section 1.10 | question limit 4,000 code points; stall timeout 45,000 ms; polling intervals; breakpoints 768 px and 1,280 px |
| Infrastructure | `83-infrastructure.md` sections 2 and 8.4 | resource names; derived zone-redundancy values; `WEBSITES_*` constants |

## Acceptance criteria

1. **Exact app-setting sets.** After every deploy the API site has exactly the 57 settings of sections 2.3, 2.4 and 2.7, the ingestion app exactly the 43 of sections 2.3, 2.5 and 2.7, and in `load` the model stub exactly the 17 of sections 2.6 and 2.7, each value equal to the catalogue's value for that environment where the catalogue states one. Proof: the deployed app-settings comparison test, step `app-settings-check` of every deploy job in `dev`, `prod` and `load`.
2. **Owner defaults file.** `infra/config/app-settings.json` has exactly the keys `api`, `ingestion` and `modelStub`, each holding exactly the **def** settings of section 2 with the values of section 2.2, and every value validates against its settings class. Proof: the `infra/` unit test of the app-settings defaults.
3. **Settings catalogue.** Every field of the settings classes of section 2.1 appears in sections 2.3 to 2.6 or 2.11 with the same name, type, default and range, and every `DOCCHAT_*` name in those sections is a field of exactly the classes the table names. Proof: the settings catalogue unit test, which reads this file's tables.
4. **Start-up refusals.** With `DOCCHAT_LOCAL_EMULATORS=true` and `WEBSITE_SITE_NAME` set, the API exits with code 78 and the record names `local_emulators_in_azure`; with the flag `false` and `DOCCHAT_SEARCH_ENDPOINT=http://127.0.0.1:9`, it exits with code 78 naming `insecure_endpoint`; the ingestion worker loads no function in both cases. With `FUNCTIONS_WORKER_PROCESS_COUNT` 5 and `DOCCHAT_INGESTION_PARSE_SLOTS_PER_PROCESS` 4, or with an isolation probe whose filter can't load, the ingestion worker loads no function and logs `parse_pool_exceeded` or `parse_isolation_failed`. Proof: the start-up refusal unit tests of `docchat_core.settings`, `apps/api` and `docchat_ingestion`; the isolation start-up check tests.
5. **Identity checks at start-up.** The API refuses to start for `DOCCHAT_CORE_ENVIRONMENT=prod` with a non-empty `DOCCHAT_AUTH_TEST_CLIENT_ID`, for two equal client IDs, for `DOCCHAT_CORE_ENVIRONMENT=local` with `WEBSITE_INSTANCE_ID` set, and for a reasoning effort outside its model's capability row; it starts with each catalogued default. Proof: the settings validation unit tests of section 6.1 checks 4 to 8.
6. **Ops-config seeding.** Applying every migration to an empty database yields exactly the 73 rows of sections 3.4 to 3.8, each with `value_json` = `default_json` = `{"value": <the default>}`. Proof: the migration test of `app.ops_config`.
7. **Ops-config ranges.** For every key, the area model accepts the default and both range ends and refuses one step outside each end, and each cross-field rule of sections 3.5 and 3.7 refuses a breaking combination. Proof: the ops-config area model unit tests.
8. **Refresh and invalid values.** A value changed in SQL is used within 60 s plus one request; an invalid stored value leaves the last valid area in use, increments `docchat.config.invalid` with `area` and `key`, and emits `ops_config_invalid`; with SQL unreachable the snapshot stays and the next attempt is no sooner than 15 s later. Proof: the `OpsConfigStore` unit tests with a fake clock and a fake SQL reader.
9. **The operator command.** `docchat-ops config set` refuses an unknown key, an out-of-range value and a reason outside 10 to 500 characters with exit 2 and no write; a stale `row_version` exits 6 with no write; a success writes the row and one `ops_config.set` audit row in one transaction; `config reset` writes `ops_config.reset`; a service-principal caller in `dev` or `prod` exits 3. Proof: the `docchat-ops config` integration tests against the local SQL Server.
10. **Capacity invariant.** With the defaults and the `prod` capacities 500 and 530, `docchat-ops chat capacity-check` prints shares of 24% and 24.8% and exits 0; `--set ratelimit.chat_requests_per_minute=4` exits 2 in `prod`; empty capacity outputs exit 0 in `dev` and 4 in `prod`. Proof: the capacity check unit tests and the `postprovision` run on every `prod` deploy.
11. **Typed environment files.** `bicep build` and `bicep build-params` pass for all three environment files; adding an undeclared member to any one, or removing a declared one, fails the build. Proof: the infrastructure gate of `ci.yaml` and its negative fixture test.
12. **Deployment type per deployment.** In the compiled template for `dev` and `prod`, each of the five Azure OpenAI deployments has `sku.name` `GlobalStandard` taken from its own type field, and changing one type field in a copy of an environment file changes that deployment's `sku.name` only. Proof: the Bicep template unit test of `openai-deployments.bicep`.
13. **Per-type preflight.** For each deployment, preflight checks 6 and 7 query the model and quota of that deployment's own type, and a type the region doesn't offer fails `model_sku_not_offered` naming the deployment. Proof: the preflight unit tests with recorded Models API and model-capacity answers.
14. **Embedding catalogue setting.** The compiled value of `DOCCHAT_OPENAI_EMBEDDING_MODELS` for the default entry equals section 2.3's JSON byte for byte, and `EmbeddingModelCatalog` refuses an entry with an extra member or a `defaultDimensions` outside `dimensions`. Proof: the Bicep output unit test and the `EmbeddingModelCatalog` unit tests.
15. **Web configuration.** The generator writes `config.json` with exactly the four members of section 5.1 and exits 1 writing nothing for each invalid input; the client shows the Configuration error screen for an extra member, a non-origin `apiBaseUrl` and a `404`; after each web deploy `/config.json` answers `200` with the expected `apiBaseUrl`. Proof: the generator unit tests, the client's configuration loading tests and the web deploy step's check.
16. **No secrets.** No deployed app setting value matches `AccountKey=`, `SharedAccessSignature`, `sig=`, `Password=` or `client_secret`, except the Key Vault reference of fallback 1 when it is active; the build output contains none of the build gate's patterns; a pull request that tracks `.env`, a file under `.azure/` or `apps/web/config.local.json` fails CI. Proof: the deployed app-settings comparison test, the web build gate and the forbidden-file check.
