# ADR 0097: Azure Verified Modules and local GA modules

- **Status:** Accepted
- **Date:** 2026-10-21; amended 2026-10-30 (the CI runner group's network settings only under the private-networking option, ADR 0122)
- **Decided by:** Architect
- **Specified in:** 01-repository.md, 83-infrastructure.md, 84-testing.md, 86-operations.md
- **Related ADRs:** 0009, 0016, 0096, 0098, 0102, 0103, 0106, 0107, 0122

## Context

Every Azure resource of every environment is declared in Bicep, and the templates take their modules from Azure Verified Modules (AVM) wherever a module exists, so that private endpoints, diagnostic settings, role assignments and secure defaults are wired the same way everywhere. The product uses GA APIs wherever one exists and records each preview dependency with its GA alternative (ADR 0009), so a reused module must not bring a preview API in with it. Every module's version, its API versions and its defaults must be visible in review, because a module default that differs from the design (zone redundancy, client affinity, a Key Vault SKU, a search replica count) changes a resource without anyone writing that change.

Vendor facts (AVM index and module sources, checked 2026-10-14; ARM template reference, checked 2026-10-14 and 2026-10-15):

- Modules are referenced as `br/public:avm/res/<path>:<version>`. Every module defaults `enableTelemetry` to `true`, which adds an empty `Microsoft.Resources/deployments` telemetry deployment named after the module's prefix.
- `document-db/database-account` 0.21.1 deploys `2026-04-01-preview` for every Cosmos DB type and sets serverless only through the preview-only `capacityMode`; the latest GA `databaseAccounts` API is 2026-03-15. `container-registry/registry` 0.13.1 deploys `2025-06-01-preview` (latest GA 2025-11-01) and defaults `networkRuleBypassOptions` to `AzureServices`. `consumption/budget/rg-scope` 0.1.1 deploys `2023-11-01` (latest GA 2026-06-01) and recomputes its default start date from `utcNow()` on every deployment.
- `web/site` defaults `clientAffinityEnabled` to `true`; `search/search-service` defaults `replicaCount` 3, `sku` `standard` and `publicNetworkAccess` `Enabled`; `cognitive-services/account` 0.19.1 deploys `accounts@2025-06-01` and has no RAI-policy parameter.
- `Microsoft.Insights/diagnosticSettings`: the latest version is `2021-05-01-preview`, and the GA versions `2016-09-01` and `2015-07-01` lack `logAnalyticsDestinationType` (resource-specific tables) and `categoryGroup`; every AVM module in use deploys `2021-05-01-preview`.
- Dependabot's package ecosystems include `docker`, `github-actions`, `npm`, `terraform` and `uv`, and none for Bicep (checked 2026-10-21).

## Decision

1. **Reference form.** Every AVM call is `br/public:avm/res/<path>:<version>` with an exact version and `enableTelemetry: false`.
2. **The pins** (versions confirmed 2026-10-14):

   | AVM module | Version | Main API | Used for |
   |---|---|---|---|
   | `web/site` | 0.24.0 | 2025-03-01 | API, function app, model stub |
   | `web/serverfarm` | 0.7.0 | 2025-03-01 | the three plans |
   | `web/static-site` | 0.9.6 | 2025-03-01 | Static Web App |
   | `sql/server` | 0.22.1 | 2025-01-01 | SQL server and database (plus a local `master` diagnostic setting) |
   | `storage/storage-account` | 0.33.1 | 2025-06-01 | both storage accounts |
   | `search/search-service` | 0.13.0 | 2025-05-01 | AI Search |
   | `cognitive-services/account` | 0.19.1 | 2025-06-01 | Azure OpenAI, Content Safety and Document Intelligence accounts, with the `deployments` parameter left empty |
   | `key-vault/vault` | 0.14.2 | 2026-02-01 | Key Vault |
   | `operational-insights/workspace` | 0.16.1 | 2025-07-01 | Log Analytics |
   | `insights/component` | 0.8.0 | 2020-02-02 | Application Insights |
   | `insights/action-group` | 0.8.0 | 2023-01-01 | action groups |
   | `network/virtual-network` | 0.10.2 | 2025-05-01 | networks and subnets |
   | `network/private-dns-zone` | 0.8.1 | 2020-06-01 | private DNS zones |
   | `network/private-endpoint` | 0.12.1 | 2025-05-01 | private endpoints |
   | `managed-identity/user-assigned-identity` | 0.6.0 | 2024-11-30 | workload and pipeline identities |

3. **AVM defaults always overridden.** Every call sets explicitly each property whose module default differs from the design: `zoneRedundant` on every plan (the module's default `true` for P and EP SKUs; the API plan takes `api.zoneRedundant`, default `false`, and the ingestion and stub plans `false`); `clientAffinityEnabled: false` on every site; `sku: 'standard'` on Key Vault (default `premium`); `sku`, `replicaCount`, `partitionCount` and `publicNetworkAccess` on AI Search from the environment file and the allowlist rule (ADR 0098). 83-infrastructure.md lists every property each call sets.
4. **Local modules where the AVM module deploys a preview API or the wrong behaviour.** `infra/modules/data/cosmos.bicep` on GA `Microsoft.DocumentDB/databaseAccounts@2026-03-15` (and its child types at that version), serverless through `capabilities: [{name: 'EnableServerless'}]` in `dev`; `infra/modules/registry/registry.bicep` on GA `Microsoft.ContainerRegistry/registries@2025-11-01` with `networkRuleBypassOptions: 'None'`; `infra/modules/monitoring/budget.bicep` on GA `Microsoft.Consumption/budgets@2026-06-01`, whose start date is the `budgetStartDate` parameter the `preprovision` hook sets once (ADR 0096).
5. **Declared directly on GA APIs, with no AVM module:** role assignments (`Microsoft.Authorization/roleAssignments@2022-04-01`), Cosmos DB data-plane role assignments (`sqlRoleAssignments@2026-03-15`), network security groups (`Microsoft.Network/networkSecurityGroups@2025-09-01`), RAI policies and model deployments (`Microsoft.CognitiveServices/accounts/raiPolicies@2026-07-01`, `accounts/deployments@2026-07-01`), the SQL audit specifications (`servers/extendedAuditingSettings` and `servers/databases/extendedAuditingSettings@2025-01-01`), the CI runner group's network settings (`GitHub.Network/networkSettings@2024-04-02`, declared only under the private-networking option of ADR 0122) and, under content-share fallback 1 only, the NAT gateway and its public IP (`Microsoft.Network/natGateways` and `publicIPAddresses@2025-09-01`, ADR 0103). The app registrations use the Microsoft Graph Bicep extension `microsoftgraph/v1.0:1.0.0` in the local module `infra/modules/identity/entra-apps.bicep`, compiled inline, never published as a template spec or registry module (ADR 0016).
6. **The one preview dependency.** `Microsoft.Insights/diagnosticSettings@2021-05-01-preview`, used by every AVM module and by the local `infra/modules/monitoring/diagnostics.bicep`, because only that version sets `logAnalyticsDestinationType: 'Dedicated'`, and the audit tables the product queries (`CDBDataPlaneRequests`, `SQLSecurityAuditEvents`, `AZKVAuditLogs`) exist only in resource-specific mode. GA alternative: version `2016-09-01` with explicit categories into the `AzureDiagnostics` table, every query and alert moved to its columns. The GA-over-preview register (ADR 0009) carries the entry and its exit condition. No other preview ARM API version appears in any compiled template.
7. **Moving a pin.** Dependabot has no Bicep ecosystem, so pins move by hand: each quarter (runbook R17) a maintainer compares every pin with the AVM index, reads the changelog of each newer version for API-version and default changes, and moves the pins in one pull request that passes the infrastructure gate (`bicep build`, `bicep build-params` for the three environments, lint, PSRule and the what-if against `dev`). A release that deploys a preview API version is not adopted; a release that moves diagnostic settings to a GA version that selects resource-specific tables is adopted at once and retires item 6.

## Alternatives considered

- **No AVM: every resource declared directly.** Rejected: the modules bring tested wiring for private endpoints, diagnostic settings and role assignments, and secure defaults such as `disableLocalAuth: true` on the AI services and AI Search, which every resource would otherwise repeat by hand.
- **The AVM Cosmos DB and registry modules despite their preview APIs.** Rejected: the product uses GA APIs wherever they exist, and the Cosmos DB module can set serverless only through a preview-only property.
- **The AVM budget module.** Rejected: its start date moves to the current month on every deployment, while a budget keeps the start date it was created with.
- **Telemetry left on.** Rejected: every module call would add an empty deployment to the deployment history and the what-if output and send usage data to Microsoft that this product has no need to send.
- **Publishing local modules to a private registry or as template specs.** Rejected: they compile inline with the templates that use them, so a change is reviewed in one pull request with no publishing step or extra registry permission.
- **GA diagnostic settings with `AzureDiagnostics` now.** Rejected (ADR 0009): every Cosmos DB, SQL audit and Key Vault query and alert would read the shared `AzureDiagnostics` table, not the resource-specific tables the product queries.

## Consequences

- Easier: modules arrive with consistent private-endpoint, diagnostic and role wiring; each pin, API version and overridden default is visible in review; no hidden deployments appear in history.
- Harder: pins move only by hand, and AVM's 0.x versions can change defaults or parameters in a minor release, so every move needs the changelog read and a clean what-if.
- Obligations: 83-infrastructure.md carries the pin table with each main API version and the properties each call sets; runbook R17 includes the quarterly AVM pin review; a CI check fails when a compiled `main.json` or `shared.json` holds a resource API version ending in `-preview` other than `Microsoft.Insights/diagnosticSettings@2021-05-01-preview`, or when a module call omits `enableTelemetry: false`; a CI test proves the compiled Cosmos DB module uses only `Microsoft.DocumentDB/databaseAccounts@2026-03-15` and its child types at that version and sets no `diagnosticLogSettings`.
- Risk: Microsoft could retire `2021-05-01-preview` before a GA successor selects resource-specific tables; the move to `AzureDiagnostics` changes every audit query, so ADR 0009 tracks it as planned work.

## References

- AVM Bicep resource module index: https://azure.github.io/Azure-Verified-Modules/indexes/bicep/bicep-resource-modules/ (checked 2026-10-14)
- AVM module sources (API versions, `enableTelemetry` and other defaults): https://github.com/Azure/bicep-registry-modules/tree/main/avm/res (checked 2026-10-14)
- AVM Cosmos DB module 0.21.1: https://github.com/Azure/bicep-registry-modules/blob/avm/res/document-db/database-account/0.21.1/avm/res/document-db/database-account/main.bicep (checked 2026-10-14)
- AVM budget module (rg-scope): https://github.com/Azure/bicep-registry-modules/blob/main/avm/res/consumption/budget/rg-scope/main.bicep (checked 2026-10-14)
- ARM template reference (latest GA API versions): https://learn.microsoft.com/en-us/azure/templates/ (checked 2026-10-14)
- Diagnostic settings template reference: https://learn.microsoft.com/en-us/azure/templates/microsoft.insights/diagnosticsettings (checked 2026-10-15)
- Cosmos DB `databaseAccounts` 2026-03-15: https://learn.microsoft.com/en-us/azure/templates/microsoft.documentdb/2026-03-15/databaseaccounts (checked 2026-10-14)
- Dependabot supported ecosystems: https://docs.github.com/en/code-security/dependabot/ecosystems-supported-by-dependabot/supported-ecosystems-and-repositories (checked 2026-10-21)
