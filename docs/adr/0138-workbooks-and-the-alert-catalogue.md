# ADR 0138: Workbooks and the alert catalogue

- **Status:** Accepted
- **Date:** 2026-10-21
- **Decided by:** Architect
- **Specified in:** 85-observability.md, 83-infrastructure.md, 86-operations.md, 84-testing.md
- **Related ADRs:** 0009, 0014, 0023, 0058, 0137, 0139, 0142, 0145

## Context

Operators must see, and be told about, API availability and 5xx rate, time to first token, Azure OpenAI 429s and latency, ingestion failures and queue age, quota rejections and the monthly budget. Every alert needs a receiver, an expected response and a runbook, or it trains people to ignore it. An unhealthy instance must leave rotation and page someone, and a failure inside a stream that already answered `200` is invisible to any status-code alert, so turn outcomes need their own rule.

Vendor facts: Azure Workbooks deploy through Bicep as `Microsoft.Insights/workbooks@2023-06-01`; log search alerts as `Microsoft.Insights/scheduledQueryRules@2026-03-01` (severity 0 to 4, `autoMitigate`, `muteActionsDuration`); metric alerts as `Microsoft.Insights/metricAlerts@2026-01-01`, whose `windowSize` must be one of PT1M, PT5M, PT15M, PT30M, PT1H, PT6H, PT12H or P1D and `evaluationFrequency` one of PT1M, PT5M, PT15M, PT30M or PT1H. A stateful log alert resolves after its condition stays false for three periods at a 5 to 15 minute frequency and two periods at 15 minutes to 11 hours. Azure Monitor keeps histograms as sum, count, minimum and maximum, so percentiles come from span durations or span attributes. The Logs query API is `POST https://api.loganalytics.io/v1/workspaces/{workspace ID}/query` with an Entra token; the Azure CLI's query commands come from the `log-analytics` (1.0.0b2) and `application-insights` (2.0.0b2) extensions, listed as Preview.

## Decision

1. **Nine workbooks per environment:** `availability` (API availability and 5xx rate), `ttft` (time to first token), `openai` (Azure OpenAI 429s and latency), `ingestion` (ingestion failures and queue age), `quota` (quota and rate-limit rejections), `budget` (monthly budget and estimated model cost), `capacity` (CPU, instances, event-loop lag, SQL and Cosmos DB), `conversations` and `access`. `infra/modules/monitoring/workbooks.bicep` deploys each with `kind: 'shared'`, `category: 'workbook'`, `name: guid(resourceGroup().id, 'docchat-workbook', <key>)`, `displayName: 'DocChat <env> - <title>'`, `sourceId` = the environment's Log Analytics workspace and `serializedData` = `loadTextContent('workbooks/<key>.workbook.json')` with the placeholders `{workspaceId}`, `{apiSiteId}`, `{ingestionSiteId}`, `{apiPlanId}`, `{autoscaleId}`, `{budgetId}` and `{apiPrincipalId}` replaced through `replace()`. Every workbook has a time-range parameter (default 24 hours) that each query applies as `TimeGenerated {TimeRange}`; request counts use `sum(ItemCount)`, so a sampling ratio below 1 still gives true counts; the budget tile is the only non-KQL tile (an Azure Resource Manager query of the budget).
2. **Alert rules:** `infra/modules/monitoring/alerts.bicep`, in every environment, names each rule `docchat-<env>-<slug>`; `enabled` comes from `alerts.enabled` in `infra/env/<env>.json` (`true` in `dev` and `prod`, `false` in `load`, where tests cause alert conditions on purpose); the one action is the ops action group `ag-docchat-<env>-ops`; each `description` starts with the alert ID and runbook (`A16 · R18`). Log rules use `kind: 'LogAlert'` on the workspace and fire when the query returns at least one row (`timeAggregation: 'Count'`, `operator: 'GreaterThan'`, `threshold: 0`, one evaluation period, one failing period). State alerts set `autoMitigate: true`; event alerts set `autoMitigate: false` and `muteActionsDuration` equal to the window. Thresholds are constants in the module except three environment-file fields, because time to first token and model latency depend on the models chosen: `alerts.ttftP95Ms` (default 20,000 ms), `alerts.openAiFirstChunkP95Ms` (default 10,000 ms) and `alerts.openAiCallMeanMs` (default 8,000 ms).
3. **The catalogue** (window / frequency, Azure Monitor severity, runbook; "of N" means the rule needs at least N events in the window):

   | ID | Slug | Fires when | Window / freq | Sev | Runbook |
   |---|---|---|---|---|---|
   | A01 | `api-down` | metric `HealthCheckStatus` average < 1 | PT5M / PT1M | 1 | R07 |
   | A02 | `api-instance-unhealthy` | metric `HealthCheckStatus` average < 100 | PT15M / PT5M | 2 | R07 |
   | A03 | `api-5xx-high` | over 10% of API requests 5xx, of 20 | PT5M / PT1M | 1 | R07 |
   | A04 | `api-5xx-elevated` | over 1% 5xx, of 50 | PT15M / PT5M | 2 | R07 |
   | A05 | `user-provisioning-failures` | failed first-sign-in provisioning over 1% of requests, of 50 | PT5M / PT5M | 2 | R07 |
   | A06 | `ttft-p95` | p95 of `docchat.ttft_ms` above `alerts.ttftP95Ms`, of 10 turns | PT15M / PT5M | 2 | R09 |
   | A07 | `chat-failed-turns` | over 5% of turns failed (stream `error` included), of 20 | PT15M / PT5M | 2 | R07 |
   | A08 | `chat-openai-throttling` | throttled chat-path model calls over 5%, of 20 | PT10M / PT5M | 2 | R09 |
   | A09 | `screening-failures` | source-screening failures over 2% of turns, of 20 | PT10M / PT5M | 2 | R07 |
   | A10 | `usage-recording-failures` | any usage-recording failure | PT15M / PT5M | 2 | R07 |
   | A11 | `prices-missing` (prod only) | any model call without a price | PT1H / PT1H, mute P1D | 3 | R09 |
   | A12 | `quota-rejections` | more than 20 daily or monthly quota rejections | PT1H / PT15M | 3 | R03 |
   | A13 | `rate-rejections-share` | rate and concurrency rejections over 10% of chat requests, of 50 | PT30M / PT15M | 3 | R03 |
   | A14 | `content-filter-volume` | more than 20 content-filter events | PT1H / PT1H, mute PT1H | 3 | R07 |
   | A15 | `uploads-scan-gate-closed` (when scanning is on) | no `clean` scanner canary result in 48 hours | P2D / PT1H | 2 | R12 |
   | A16 | `ingestion-item-failures` | more than 5 failed items | PT15M / PT5M, mute PT15M | 2 | R18 |
   | A17 | `ingestion-queue-age` | oldest queued item older than 1,800 s | PT15M / PT5M | 2 | R18 |
   | A18 | `ingestion-dispatcher-stale` | items queued and dispatcher heartbeat older than 900 s | PT15M / PT5M | 2 | R18 |
   | A19 | `ingestion-poison-messages` | poison queue not empty | PT2H / PT1H | 2 | R18 |
   | A20 | `malware-detected` | any `malware_detected` event | PT5M / PT5M, mute PT5M | 2 | R18, R07 |
   | A21 | `ingestion-throttling` | more than 50 throttled ingestion calls | PT5M / PT5M | 3 | R18 |
   | A22 | `ingestion-run-failed` | any run finished `failed` | PT15M / PT15M, mute PT15M | 2 | R18 |
   | A23 | `telemetry-silent` | no metric from `docchat-api` or `docchat-ingestion` | PT30M / PT15M | 2 | R07 |
   | A24 | `audit-write-failures` | any failed audit write | PT15M / PT5M | 2 | R07 |
   | A25 | `summary-failures` | over 20% of summary rebuilds failed, of 20 | PT1H / PT15M | 3 | R19 |
   | A26 | `extraction-failures` | over 20% of memory extractions failed, of 20 | PT1H / PT15M | 3 | R19 |
   | A27 | `abandoned-turns` | abandoned turns over 5% of finalised turns, of 20 | PT1H / PT15M | 2 | R19 |
   | A28 | `client-id-conflicts` | more than 10 client turn ID conflicts | PT1H / PT15M | 3 | R19 |
   | A29 | `human-conversation-access` | any Cosmos DB data-plane request on `docchat` by a principal other than the API identity | PT15M / PT15M, mute PT15M | 3 | R19 |
   | A30 | `event-loop-lag` | an instance's loop lag above 500 ms in at least 5 one-minute bins | PT15M / PT5M | 3 | R07 |
   | A31 | `sql-failures` | more than 5 SQL failures per role | PT5M / PT5M | 2 | R07 |
   | A32 | `telemetry-scrub-errors` | any scrubbing error | PT15M / PT15M | 2 | R07 |
   | A33 | `telemetry-dropped-attributes` | any dropped telemetry attribute | PT1H / PT1H | 3 | R07 |
   | A34 | `api-capacity-exhausted` | plan CPU above 85% at the maximum instance count | PT15M / PT5M | 2 | R07 |
   | A35 | `signing-key-fetch-failures` | at least 3 signing-key refresh failures | PT1H / PT15M | 2 | R07 |
   | A36 | `signing-key-age` | an instance's signing keys older than 14,400 s | PT15M / PT15M | 2 | R07 |
   | A37 | `role-not-assigned` | more than 30 `role_not_assigned` denials | PT15M / PT5M | 3 | R13 |
   | A38 | `invalid-token-share` | invalid tokens over 5% of validations, of 100 | PT15M / PT5M | 3 | R07 |
   | A39 | `ops-config-invalid` | any invalid ops-config value | PT15M / PT15M | 2 | R03 |
   | A40 | `index-override-used` (prod only) | daily summary of per-user index override use | P1D / P1D, mute P1D | 3 | R04 |
   | A41 | `openai-first-chunk-latency` | answer call's first-chunk p95 above `alerts.openAiFirstChunkP95Ms`, of 20 calls | PT15M / PT5M | 2 | R09 |
   | A42 | `openai-call-latency` | mean of a non-answer model call above `alerts.openAiCallMeanMs`, of 20 calls | PT15M / PT5M | 2 | R09 |
   | A43 | `request-limit-rejections` | more than 200 burst or write-limit rejections | PT15M / PT5M | 3 | R07 |
   | A58 | `rerender-failed` | any `rerender` item failed with `render_failed` or `render_too_large` (ADR 0145 decision 10) | PT1H / PT1H, mute PT1H | 3 | R18 |

   The component that owns a rule's telemetry writes its query (A24 belongs to conversations and memory, on the counter `docchat.audit.write_failures`); the exact KQL of every rule and tile is in `85-observability.md`.
4. **Severities and response.** Sev1 (Error): users can't use a core function (sign-in, chat, citations, uploads) or a security control failed open; acknowledge within 30 minutes during the organisation's cover hours and follow R07. Sev2 (Warning): a degradation with users still served; act the same business day. Sev3 (Informational): a trend, hygiene item or daily summary; act the next business day. Severities 0 and 4 aren't used.
5. **Rules owned elsewhere** are listed in the catalogue and not duplicated: the budget notifications `actual-50`, `actual-80` and `actual-100` on `budget-docchat-<env>` to the ops action group, and the security alerts in `log-docchat-security` and the Activity Log on the pipeline identities, app registrations and just-in-time groups, to `ag-docchat-security`.
6. **KQL outside workbooks and alerts** (load verdicts, the query check, runbooks, `docchat-ops trace show`) goes through the Logs query API v1 with a token for `https://api.loganalytics.io/.default` (`docchat_testing.logquery.query_logs`, timeout 30 s), never through a CLI extension: every workflow runs `az config set extension.use_dynamic_install=no` after signing in.

## Alternatives considered

- **Azure Managed Grafana or portal dashboards.** Rejected: Grafana adds a paid resource and its own access model; portal dashboards don't carry KQL tiles as reviewable files; workbooks are GA, Bicep-deployable and query the workspace the alerts use.
- **Application Insights smart detection and recommended alert rules.** Rejected: their thresholds and receivers aren't the product's, and they don't cover ingestion, quotas or screening.
- **One threshold set for every environment and model.** Rejected for latency: time to first token and model-call latency depend on the deployed models, so those three thresholds are environment-file fields; every other threshold describes this code's behaviour and stays in the module.
- **Metric alerts for everything.** Rejected: ratios, percentiles and joins across tables need log search; metric rules are kept for the platform's health-check metric.
- **`az monitor log-analytics query` in scripts.** Rejected: the command comes from a CLI extension listed as Preview, which breaks the GA-over-preview rule (ADR 0009).
- **Alerts enabled in `load`.** Rejected: the load test causes 429s, failures and saturation on purpose, and paging operators for them would teach them to ignore pages.

## Consequences

- Every required signal has a workbook and at least one alert; failures inside a `200` stream page through A07, and an unhealthy instance through A01 and A02.
- An alert can't ship without a runbook: a unit test fails a rule whose description doesn't start with its ID and name an existing runbook file, whose action isn't the ops group, or whose metric window or frequency the platform refuses.
- Obligations: runbook R20 maps every alert ID to its first response; owners keep their rules' queries in step with their telemetry; a change that adds telemetry adds its catalogue row in the same pull request.
- Stage 6 acceptance: every alert that can be caused safely in `dev` (A16 with a corrupt PDF, A12 with a temporarily lowered quota, A37 with 40 calls by `norole`) fires to the dev ops group, and the load run's alert probe proves A41 and A42.
- Tests (84-testing.md): every workbook parses, has the time-range parameter in every query and uses only the documented placeholders; the alert rules' shape; every workbook and alert query runs without a KQL error against the dev workspace through the Logs query API; the alert probe.

## References

- Workbooks Bicep reference: https://learn.microsoft.com/en-us/azure/templates/microsoft.insights/workbooks?pivots=deployment-language-bicep (checked 2026-10-15)
- Scheduled query rules Bicep reference (2026-03-01): https://learn.microsoft.com/en-us/azure/templates/microsoft.insights/scheduledqueryrules?pivots=deployment-language-bicep (checked 2026-10-21)
- Metric alerts Bicep reference: https://learn.microsoft.com/en-us/azure/templates/microsoft.insights/metricalerts?pivots=deployment-language-bicep (checked 2026-10-15)
- Alert states and resolution: https://learn.microsoft.com/en-us/azure/azure-monitor/alerts/alerts-overview (checked 2026-10-21)
- Logs query API overview and request format: https://learn.microsoft.com/en-us/azure/azure-monitor/logs/api/overview, https://learn.microsoft.com/en-us/azure/azure-monitor/logs/api/request-format (checked 2026-10-21)
- Azure CLI extension list (status of the query extensions): https://learn.microsoft.com/en-us/cli/azure/azure-cli-extensions-list (checked 2026-10-15)
