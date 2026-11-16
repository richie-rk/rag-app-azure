# ADR 0009: GA over preview register

- **Status:** Accepted
- **Date:** 2026-10-18
- **Decided by:** Architect
- **Specified in:** 01-repository.md, 12-chat-pipeline.md, 14-ingestion.md, 83-infrastructure.md, 84-testing.md, 85-observability.md, 86-operations.md
- **Related ADRs:** 0010, 0035, 0038, 0046, 0047, 0048, 0062, 0066, 0097, 0103, 0130, 0137, 0138

## Context

The product prefers GA over preview. A dependency on a preview feature or a pre-release package is acceptable only when it is recorded with its GA alternative, and the target spec names every confirmed GA API, SDK and runtime version with the date it was confirmed. A pre-release SDK on an active path, a preview REST API version written into code, an end-of-life runtime or an unpinned build toolchain puts a production path on behaviour the vendor may change without notice. A security review needs one place that lists every exception, why it exists and what replaces it, and the builder needs to know which preview switches never to turn on.

Three kinds of exception remain after the design chose GA everywhere it could. Some dependencies have no GA form: the only in-process Application Insights exporter is a pre-release, and two libraries the design requires (`httpx`, the one first-party HTTP client, and `unstructured`, the engine of the page-wise method) are final releases that their maintainers still classify as Beta. One GA API version lacks a capability the design needs (diagnostic settings). Several services offer preview features beside the GA paths the design uses.

Vendor facts (checked 2026-10-14 and 2026-10-15):

- `Microsoft.Insights/diagnosticSettings`: the latest version is `2021-05-01-preview`; the GA versions `2016-09-01` and `2015-07-01` lack `logAnalyticsDestinationType` (resource-specific tables) and `categoryGroup`; every AVM module in use deploys `2021-05-01-preview`.
- `azure-monitor-opentelemetry` 1.8.11 is Production/Stable and depends on `azure-monitor-opentelemetry-exporter ~=1.0.0b58`, which is still Beta; since 1.0.0b36 the exporter builds the Entra credential from `APPLICATIONINSIGHTS_AUTHENTICATION_STRING`.
- The root `host.json` setting `"telemetryMode": "OpenTelemetry"` is marked "currently in preview".
- `azure-functions-durable` 1.7.0 is the latest stable release; 2.0.0b1 to 2.0.0rc3 are a separate preview line on the durabletask runtime whose README says "APIs may change before the stable 2.0.0".
- `unstructured` 0.27.25 and `unstructured-inference` 1.6.13 carry Development Status 4 - Beta, with no GA designation; `httpx` 0.28.1 (2024-12-06) is the latest stable release with classifier 4 - Beta, and `httpx2` 2.13.1 is Production/Stable.
- Preview beside GA: AVM `document-db/database-account` 0.21.1 deploys `2026-04-01-preview` and AVM `container-registry/registry` 0.13.1 deploys `2025-06-01-preview`; `policyExemptions` has only preview API versions (latest `2026-01-01-preview`); the Azure CLI's `log-analytics` 1.0.0b2 and `application-insights` 2.0.0b2 extensions are Preview; `azure-ai-contentsafety` 1.1.0b1 (beta) adds prompt shielding and Prompt Shields also has `2026-07-01-preview`; groundedness and PII content filters are preview; Defender for Storage on-demand scanning uses REST `2024-10-01-preview`; PSRule engine v3 and PSRule for Azure 1.48.0 exist only as pre-releases.

## Decision

1. **The rule.** GA means: for an Azure service, feature, REST API version or ARM resource API version, not labelled preview by Microsoft; for a package, a final release under PEP 440 (no `a`, `b`, `rc` or `.dev` segment). Item 2 lists every dependency in use outside that rule, together with the two final-release libraries still classified Beta. Item 3 lists the preview features beside the GA paths in use, none of which is ever switched on. A pull request that adds a preview API version, a preview feature or a pre-release package amends this register in the same change with its reason, its GA alternative and the condition that retires it.
2. **In use, recorded with the GA alternative.**

| Dependency | Why it is used | GA alternative | Retired when |
|---|---|---|---|
| `Microsoft.Insights/diagnosticSettings@2021-05-01-preview`, in every AVM module and the local diagnostic module | `logAnalyticsDestinationType: 'Dedicated'` creates the resource-specific tables the design queries (`CDBDataPlaneRequests`, `SQLSecurityAuditEvents`, `AZKVAuditLogs`) | GA `2016-09-01` with explicit categories into the `AzureDiagnostics` table, every query and alert moved to its columns | a GA API version selects resource-specific tables and the AVM pins move to it in a reviewed pull request (ADR 0097) |
| `azure-monitor-opentelemetry-exporter` 1.0.0b58, a dependency of the GA distro 1.8.11 | both apps export telemetry through the distro (ADR 0137), to Application Insights with Entra authentication | none in process: no GA exporter to Application Insights exists, an accepted risk | the exporter ships a final release; until then it is pinned exactly in `uv.lock` and changed only through a Dependabot pull request (group `opentelemetry`) that passes T-QUAL-21 to T-QUAL-29 |
| `unstructured` 0.27.25 with `unstructured-inference` 1.6.13 (Beta classifier) | the page-wise method runs on this engine (ADR 0047) | the recursive method on Document Intelligence layout, GA API 2024-11-30, selectable for any index (ADR 0048) | the library declares a stable status; until then versions are pinned, an update passes the fixture-document tests and the image's offline self-check, and a behaviour change ships as a new method version (ADR 0046) |
| `httpx` 0.28.1 (Beta classifier) | the one first-party HTTP client (signing keys, Prompt Shields REST, ROPC in tests, Graph in `docchat-ops`); the distro instruments it and respx mocks it | `httpx2` 2.13.1 (Production/Stable), already in the lock as the `openai` package's transport and imported only in `docchat_core.clients.openai` (ADR 0130) | the distro instruments `httpx2` or `httpx` is retired upstream; a new ADR then revisits the choice, and the swap changes only the modules that construct an `httpx.AsyncClient` |

3. **Preview features beside the GA paths in use, never switched on.**

| Preview feature | GA path used |
|---|---|
| Functions host OpenTelemetry mode (`"telemetryMode": "OpenTelemetry"`) | `configure_telemetry` runs the GA distro inside the Python worker; `PYTHON_APPLICATIONINSIGHTS_ENABLE_TELEMETRY` stays unset; `host.json` keeps Application Insights sampling and dependency tracking off (ADR 0137) |
| `azure-functions-durable` 2.x (durabletask-based) | `azure-functions-durable` 1.7.0 with `azure-functions` 2.3.0 and the pinned worker `azure-functions-runtime` 1.2.1; with no `save_events`, the dispatcher continues as new without it and treats `run_queued` events as hints (ADR 0035); 2.x is reviewed once GA |
| AVM `document-db/database-account` 0.21.1 (`2026-04-01-preview`, serverless only through the preview-only `capacityMode`) | local `infra/modules/data/cosmos.bicep` on GA `Microsoft.DocumentDB/databaseAccounts@2026-03-15` with `capabilities: [{name: 'EnableServerless'}]` |
| AVM `container-registry/registry` 0.13.1 (`2025-06-01-preview`) | local `infra/modules/registry/registry.bicep` on GA `Microsoft.ContainerRegistry/registries@2025-11-01` |
| Cosmos DB full-text query logging (an account property only in preview `databaseAccounts` versions) | not set; the GA default (off) keeps query text out of data-plane logs |
| Azure Policy exemptions (preview API versions only) | under content-share fallback 1, the shared-key Deny assignment lists the host account in `notScopes` (ADR 0103) |
| Azure CLI `log-analytics` and `application-insights` extensions | no CLI extension is installed: every job runs `az config set extension.use_dynamic_install=no`, and KQL outside workbooks and alerts goes through the GA Logs query API v1, `POST https://api.loganalytics.io/v1/workspaces/{id}/query` (ADR 0138) |
| `azure-ai-contentsafety` 1.1.0b1 and Prompt Shields `2026-07-01-preview` | `POST {endpoint}/contentsafety/text:shieldPrompt?api-version=2024-09-01` (GA) through `httpx.AsyncClient` (ADR 0062) |
| Content-filter groundedness and PII categories | the RAI policies `docchat-text-guard` and `docchat-table-summary-guard` configure only GA filters: the four harm categories, jailbreak, indirect attack and protected material (ADR 0066) |
| Defender for Storage on-demand malware scanning | `scan_required` is set per version at upload, so a version committed while scanning was off is never gated, also on rebuilds (ADR 0038) |
| PSRule engine v3 and PSRule for Azure 1.48.0 pre-releases | `ps-rule.yaml` requires `PSRule >=2.9.0 <3.0.0` and `PSRule.Rules.Azure >=1.47.0 <1.48.0`, baseline `Azure.GA_2025_12` |
| Blob index tags in Azurite 3.37.0 (preview in the emulator; test tooling only) | the scan-result tag path is proven by live tests against Azure Storage, never by the emulator alone |

4. **GA baseline, confirmed 2026-10-14.** AI Search data-plane API 2026-04-01 through `azure-search-documents` 12.0.0, whose default it is; Document Intelligence API 2024-11-30 through `azure-ai-documentintelligence` 1.0.2; Prompt Shields 2024-09-01 over REST; the Azure OpenAI v1 API (`{endpoint}/openai/v1/`) through `openai` 3.31.0; the Functions 4.x host with extension bundle `[4.39.1, 5.0.0)` (the 4.x line is active, 3.x and older are deprecated) and `azure-functions-durable` 1.7.0; Python 3.13, supported on Functions 4.x until October 2029 (ADR 0010); Node.js 24.21.0 LTS with `engines` `>=24.15.0 <25`; the Microsoft Graph Bicep extension types `microsoftgraph/v1.0:1.0.0`; every other ARM resource on a GA API version (ADR 0097). No pre-release SDK, preview REST version or end-of-life runtime sits on an active path outside item 2.
5. **Review.** An entry of item 2 is re-checked whenever the change that could retire it arrives: the Dependabot pull requests on the distro, the exporter, `httpx`, `openai` and `unstructured` (runbook R17), and any pull request that moves an AVM pin. The pull request that meets an exit condition deletes the entry.

## Alternatives considered

- **GA only, with no exceptions.** Lost: the page-wise method has no GA engine; no GA in-process exporter to Application Insights exists; and the GA diagnostic-settings versions can't create the audit tables the alerts query.
- **The GA diagnostic-settings version now, with `AzureDiagnostics` categories.** Lost: every Cosmos DB, SQL audit and Key Vault query and alert would read the shared `AzureDiagnostics` table, which the design's KQL doesn't use, for a preview version that every AVM module already deploys.
- **`httpx2` now as the first-party client.** Lost: the distro instruments `httpx` and not `httpx2`, and every adapter unit test mocks `httpx` with respx; the swap stays ready for the day either fact changes.
- **The Durable 2.x line for `save_events`.** Lost: a preview whose APIs may still change, on the orchestration path that holds every ingestion run; the GA SDK with a drain-then-reset dispatcher meets the same need.
- **No register; each component records its own previews.** Lost: a reviewer would have to read every component file to learn what isn't GA, and a new preview could enter without a stated alternative.

## Consequences

- Easier: a security review reads one list; the builder knows which preview switches never to turn on; a new preview dependency is visible in the pull request that adds it.
- Harder: some capabilities wait for GA: `save_events`, host-level OpenTelemetry, on-demand malware scans, the preview content filters.
- Obligations: 01-repository.md's version table carries the GA baseline with its confirmation dates; 83-infrastructure.md marks `2021-05-01-preview` as the one preview ARM API version; 85-observability.md records the exporter pin and the unset host mode; 14-ingestion.md records the Durable SDK line and the Beta status of the parsing libraries; T-QUAL-21 to T-QUAL-29 gate every distro and exporter update; the ingestion fixture tests and the image's offline self-check gate every `unstructured` update; every workflow sets `extension.use_dynamic_install=no`.
- Risk: the exporter is a pre-release whose behaviour can change between betas; the exact pin and the telemetry tests keep an unreviewed beta out of production.
- Risk: Microsoft could retire `2021-05-01-preview` before a GA successor exists; moving to `AzureDiagnostics` changes every query of the audit tables, so that switch is planned work tracked in 83-infrastructure.md, not an emergency fallback.
- Risk: `unstructured` can change behaviour between minor releases; pins, fixture tests and method versioning contain a change to the indexes that opt into it.

## References

- Diagnostic settings template reference (latest `2021-05-01-preview`; GA versions lack `logAnalyticsDestinationType`): https://learn.microsoft.com/en-us/azure/templates/microsoft.insights/diagnosticsettings (checked 2026-10-15)
- Azure Monitor OpenTelemetry distro for Python (1.8.11, exporter `~=1.0.0b58`): https://learn.microsoft.com/en-us/python/api/overview/azure/monitor-opentelemetry-readme?view=azure-python (checked 2026-10-14)
- Azure Monitor OpenTelemetry exporter (credential from `APPLICATIONINSIGHTS_AUTHENTICATION_STRING`): https://learn.microsoft.com/en-us/python/api/overview/azure/monitor-opentelemetry-exporter-readme?view=azure-python (checked 2026-10-14)
- Functions `host.json` reference (`telemetryMode` in preview): https://learn.microsoft.com/en-us/azure/azure-functions/functions-host-json (checked 2026-10-14)
- azure-functions-durable on PyPI (1.7.0 stable; 2.x pre-releases): https://pypi.org/project/azure-functions-durable/ (checked 2026-10-14)
- durabletask-python, `azure-functions-durable` 2.x README: https://github.com/microsoft/durabletask-python/blob/main/azure-functions-durable/README.md (checked 2026-10-14)
- unstructured on PyPI (Development Status 4 - Beta): https://pypi.org/project/unstructured/ (checked 2026-10-14)
- httpx on PyPI (0.28.1, classifier Beta): https://pypi.org/project/httpx/ (checked 2026-10-14)
- Azure Verified Modules index and module sources (preview API versions of the Cosmos DB and registry modules): https://azure.github.io/Azure-Verified-Modules/indexes/bicep/bicep-resource-modules/ and https://github.com/Azure/bicep-registry-modules/tree/main/avm/res (checked 2026-10-14)
- Azure CLI extensions list (`log-analytics`, `application-insights` in Preview): https://learn.microsoft.com/en-us/cli/azure/azure-cli-extensions-list (checked 2026-10-15)
- Prompt Shields REST reference (2024-09-01 GA): https://learn.microsoft.com/en-us/rest/api/contentsafety/text-operations/shield-prompt (checked 2026-10-14)
- azure-ai-contentsafety on PyPI (1.1.0b1 beta adds prompt shielding): https://pypi.org/project/azure-ai-contentsafety/ (checked 2026-10-14)
- Azure OpenAI content filters (groundedness and PII in preview): https://learn.microsoft.com/en-us/azure/foundry-classic/openai/how-to/content-filters (checked 2026-10-14)
- Defender for Storage on-demand malware scanning (`2024-10-01-preview`): https://learn.microsoft.com/en-us/azure/defender-for-cloud/on-demand-malware-scanning (checked 2026-10-14)
- PSRule for Azure changelog and PSRule releases: https://github.com/Azure/PSRule.Rules.Azure/blob/main/docs/changelog.md and https://github.com/microsoft/PSRule/releases (checked 2026-10-14)
