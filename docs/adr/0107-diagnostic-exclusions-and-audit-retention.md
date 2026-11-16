# ADR 0107: Diagnostic exclusions and audit retention

- **Status:** Accepted
- **Date:** 2026-10-21
- **Decided by:** Architect
- **Specified in:** 83-infrastructure.md, 85-observability.md, 82-security.md, 84-testing.md
- **Related ADRs:** 0009, 0014, 0024, 0029, 0097, 0103, 0106, 0108, 0115, 0137, 0138

## Context

Logs and traces in this product carry metadata only: never prompts, answers, document text, file names, email addresses, display names, tokens, keys or connection strings. Operator access to another user's conversations happens only through just-in-time access, so the data-plane access logs that record it must be collected and kept long enough to review. Every resource sends its diagnostics to Log Analytics, and platform logs are a second path for personal data beside the application's own scrubbed telemetry, so each category is either needed for audit or operations or left off.

Vendor facts:

- `AppServiceHTTPLogs` records, per request, `CsUsername` ("The name of the authenticated user on HTTP request"), `CIp` (client IP), `CsUriQuery` (query string), `UserAgent` and `Referer`.
- `Microsoft.Insights/diagnosticSettings`: the latest version is `2021-05-01-preview`; the GA versions `2016-09-01` and `2015-07-01` lack `logAnalyticsDestinationType` (resource-specific tables) and `categoryGroup`; every Azure Verified Module in use deploys the preview version.
- Log Analytics keeps Analytics tables for the workspace's retention unless a table sets its own: analytics retention (`retentionInDays`) from 4 to 730 days and total retention (`totalRetentionInDays`) up to 4,383 days; data past analytics retention stays in long-term retention, read through a search job, which needs `Microsoft.OperationalInsights/workspaces/tables/write` and `searchJobs/write` (Log Analytics Contributor) and is charged for the long-term data it scans; raising total retention applies to data already ingested; `AzureActivity` and the Application Insights tables keep 90 days at no charge.
- Cosmos DB logging of full query text is an account property only in preview `databaseAccounts` API versions and is off by default, so `CDBDataPlaneRequests` carries no query text.

## Decision

1. **One setting per resource.** Every resource with diagnostic categories gets one setting named `to-log-analytics` to its environment's workspace (the registry and the test-credential vault send theirs to `log-docchat-security`), with `logAnalyticsDestinationType: 'Dedicated'`, on `Microsoft.Insights/diagnosticSettings@2021-05-01-preview` (the one preview dependency, ADR 0009):

   | Resource | Log categories | Metrics |
   |---|---|---|
   | API web app | `AppServicePlatformLogs`, `AppServiceAuditLogs`, `AppServiceIPSecAuditLogs` | `AllMetrics` |
   | Ingestion function app | `FunctionAppLogs` | `AllMetrics` |
   | Model stub app (`load`) | `AppServicePlatformLogs` | `AllMetrics` |
   | App Service plans, storage accounts | none exist | `AllMetrics` (plans), `Transaction` (accounts) |
   | SQL `master` (settings `sql-audit`, and `sql-audit-security` to `log-docchat-security` in `dev` and `prod`, ADR 0108) | `SQLSecurityAuditEvents`, `DevOpsOperationsAudit` | none |
   | SQL `docchat` | `SQLSecurityAuditEvents`, `Errors`, `Timeouts`, `Blocks`, `Deadlocks` | `Basic`, `InstanceAndAppAdvanced` |
   | Cosmos DB | `DataPlaneRequests`, `ControlPlaneRequests`, `PartitionKeyRUConsumption` | `Requests` |
   | Data storage, blob and queue services | `StorageRead`, `StorageWrite`, `StorageDelete` | `Transaction` |
   | Functions host storage, blob, queue and table services | `StorageWrite`, `StorageDelete` | `Transaction` |
   | AI Search | `OperationLogs` | `AllMetrics` |
   | Azure OpenAI, Content Safety, Document Intelligence | `Audit`, `RequestResponse` | `AllMetrics` |
   | Key Vault; test-credential vault | `AuditEvent` | `AllMetrics` |
   | Autoscale setting | `AutoscaleScaleActionsLog` | `AllMetrics` |
   | Container registry (`registry-audit`) | `ContainerRegistryRepositoryEvents`, `ContainerRegistryLoginEvents` | `AllMetrics` |

   Resources with no categories (virtual networks, network security groups, private endpoints, DNS zones, GitHub network settings, managed identities, Static Web Apps, action groups, budgets) get none; under the content-share fallback the NAT gateway and its public IP get `AllMetrics` only (ADR 0103).
2. **Four exclusions.**
   - `AppServiceHTTPLogs`: every request's authenticated user name (a sign-in name, which the logging policy treats as an email address), query string, user agent and referrer would land in a log; the application's own request telemetry carries the user's object ID and the operation.
   - `AppServiceConsoleLogs`: the container's standard output and error duplicate the OpenTelemetry logs and would carry, unscrubbed, anything a library prints; the scrubbing filter runs only in the telemetry pipeline.
   - `AppServiceAuthenticationLogs`: their content is undocumented, so nothing proves they hold no tokens or user identifiers.
   - `StorageRead` on the Functions host account: Durable Functions polls its queues and tables continuously, and those reads carry no audit value.
3. **Audit logs keep their source addresses.** `AppServiceIPSecAuditLogs`, `StorageBlobLogs`, `CDBDataPlaneRequests`, `SQLSecurityAuditEvents`, `AZKVAuditLogs` and the Activity Log record caller IP addresses and principal IDs; they are kept, because an access record without its source can't support an investigation, and they never carry document text, prompts or answers (storage paths are generated IDs; Cosmos DB records carry no query text).
4. **Retention.** Workspace `retentionInDays`: 30 in `dev` and `load`, 90 in `prod`. In `prod` the audit tables `CDBDataPlaneRequests`, `SQLSecurityAuditEvents`, `AZKVAuditLogs`, `StorageBlobLogs` and `AppServiceAuditLogs` are declared as `Microsoft.OperationalInsights/workspaces/tables` with `retentionInDays: 90` and `totalRetentionInDays: 365`. `log-docchat-security` keeps everything 365 days of analytics retention: the Activity Log, Microsoft Entra audit and sign-in logs, the test vault's audit events, the registry's events and the privileged SQL audit of `dev` and `prod`.
5. **No dropped audit data.** `dailyQuotaGb` is 2 in `dev` and `-1` (no cap) in `prod` and `load`, so production audit records are never dropped and a load run's telemetry keeps flowing.

## Alternatives considered

- **`AppServiceHTTPLogs` with the column that holds the user name removed by a transformation.** Rejected: a workspace transformation would be one more component to keep correct, and the application's own request telemetry already gives every field an investigation needs without personal data.
- **Category groups (`allLogs`, `audit`).** Rejected: a group gains categories when the platform adds them, so a new category could start collecting personal data with no repository change.
- **The `AzureDiagnostics` table through a GA API version.** Rejected: the audit queries and alerts read the resource-specific tables (`CDBDataPlaneRequests`, `SQLSecurityAuditEvents`, `AZKVAuditLogs`), which only `logAnalyticsDestinationType: 'Dedicated'` produces; it stays the recorded GA alternative (ADR 0009).
- **365 days of analytics retention for the audit tables.** Rejected: monthly access reviews read 31 days; 90 days of interactive data covers those and recent investigations at a lower cost, and long-term retention keeps the rest.
- **A daily cap in `prod`.** Rejected: a reached cap stops ingestion of audit records until the next day.

## Consequences

- No platform log in any environment records a user's sign-in name, a request's query string or the containers' raw console output.
- Reading `prod` audit data older than 90 days is a search job, which needs Log Analytics Contributor on the workspace and is charged by the data scanned; no standing operator role holds it, so the bootstrap operator (Owner) runs such searches for an investigation. `log-docchat-security` stays interactive for its full 365 days.
- The diagnostic-settings API is a preview dependency, recorded in ADR 0009 with its GA alternative; an Azure Verified Module release that moves to a newer GA version is adopted through the quarterly dependency review (R17).
- Deleting any of these settings, or changing one outside a deploy-identity deployment, raises `docchat-alert-diagnostic-settings` (ADR 0115).
- Tests (84-testing.md): the deployed coverage test of ADR 0106 checks that every resource of the table has exactly its categories and that none of the four excluded categories is set; the operational checks read `CDBDataPlaneRequests` and `StorageMalwareScanningResults` from the resource-specific tables.

## References

- `AppServiceHTTPLogs` table: https://learn.microsoft.com/en-us/azure/azure-monitor/reference/tables/appservicehttplogs (checked 2026-10-14)
- Diagnostic settings resource and API versions: https://learn.microsoft.com/en-us/azure/templates/microsoft.insights/diagnosticsettings (checked 2026-10-15)
- Log Analytics data retention: https://learn.microsoft.com/en-us/azure/azure-monitor/logs/data-retention-configure (checked 2026-10-21)
- Search jobs: https://learn.microsoft.com/en-us/azure/azure-monitor/logs/search-jobs (checked 2026-10-21)
- Azure SQL auditing to Log Analytics: https://learn.microsoft.com/en-us/azure/azure-sql/database/auditing-setup?view=azuresql (checked 2026-10-14)
