# ADR 0053: Ingestion concurrency and plan sizing

- **Status:** Accepted
- **Date:** 2026-10-18
- **Decided by:** Architect
- **Specified in:** 14-ingestion.md, 81-config.md, 83-infrastructure.md, 84-testing.md, 85-observability.md, 86-operations.md
- **Related ADRs:** 0005, 0034, 0036, 0049, 0103, 0134, 0144

## Context

- Parsing is CPU- and memory-heavy, and the parsing library holds a process-wide lock while it renders PDFs, so ingestion parallelism has to come from processes and instances, never threads. Each instance is sized from the measured memory of one parsing process, starting at 2 GB.
- One dispatcher enforces the global limit `ingestion.max_parallel_items` (default 1) across all runs (ADR 0034). Durable Functions' own concurrency settings act per host instance only, so they must match what one instance can really run, with the instance count capped in the plan; both are ceilings above the dispatcher's limit.
- Every parse runs in a fresh child process limited to `DOCCHAT_INGESTION_PARSE_MEMORY_LIMIT_MB` (default 2,048) and `DOCCHAT_INGESTION_PARSE_TIMEOUT_SECONDS` (default 1,200) per part (ADR 0049), inside activity deadlines of up to 2,700 s (ADR 0144). The keep-alive's watchdog restarts the dispatcher when its last sweep is older than 10 minutes while work is outstanding (ADR 0036), so a sweep must never wait behind a 20-minute parse.
- Vendor facts (checked 2026-10-14):
  - Elastic Premium: EP1 1 core and 3.5 GB, EP2 2 cores and 7 GB, EP3 4 cores and 14 GB; every plan bills at least one instance; always-ready instances set the plan minimum and bill continuously; scaling beyond the minimum is best effort; `maximumElasticWorkerCount` caps the burst and `functionAppScaleLimit` can't exceed it.
  - `FUNCTIONS_WORKER_PROCESS_COUNT` defaults to 1 (at most 10) and applies to every scaled-out host; for CPU-bound Python apps Microsoft advises at least as many worker processes as cores and few threads.
  - Durable `maxConcurrentActivityFunctions` and `maxConcurrentOrchestratorFunctions` default to 10 × processors on Premium, per host instance. Microsoft's guidance sets them to the app's real Python concurrency, because activities an instance accepts beyond it stall there and can't be rebalanced to other workers.
  - App settings whose names contain a period aren't supported on Linux Elastic Premium.
  - Document Intelligence S0 accepts 15 analyze transactions per second by default.

## Decision

1. **One parse slot per worker process.** `DOCCHAT_INGESTION_PARSE_SLOTS_PER_PROCESS` (app setting, default 1, range 1 to 4) is a per-process semaphore around the parsing child; the SKU map below assumes 1. Each slot holds two of the 16 parse users (ADR 0049 decision 14), so worker processes × slots stays at most 8, which the ingestion app checks when it starts, loading no function otherwise.
2. **The SKU map.** `main.bicep` derives three app settings from `ingestion.sku`, never as parameters, so a SKU change moves them together:

   | Setting | EP1 | EP2 | EP3 |
   |---|---|---|---|
   | `FUNCTIONS_WORKER_PROCESS_COUNT` | 1 | 2 | 4 |
   | `AzureFunctionsJobHost__extensions__durableTask__maxConcurrentActivityFunctions` | 2 | 4 | 8 |
   | `AzureFunctionsJobHost__extensions__durableTask__maxConcurrentOrchestratorFunctions` | 2 | 4 | 8 |

   The names contain no period, so Linux Elastic Premium accepts them. `host.json` carries the EP1 values (2 and 2) for local runs; the app settings replace them in Azure.
3. **Two activity slots per parse slot.** Activities are `async def`, and a parse only awaits its child process, so one worker process runs its parse and another activity at once. Each instance runs as many parses as it has worker processes and twice as many activities, so the dispatcher's `load_dispatch_settings`, `sweep_ingestion_state` and `claim_ingestion_items` find a free slot beside parses that can take 20 minutes, and the watchdog never restarts a dispatcher whose sweep was queued behind one. Orchestrator slots equal activity slots; orchestrator episodes only replay history and do no I/O.
4. **Memory.** EP1 (1 vCPU, 3.5 GB) runs 1 parse; EP2 (2 vCPU, 7 GB) runs 2: 2 × 2,304 MB of children (2,048 MB for the parse and at most 256 MB for a `verify-render` child beside a `render` child, ADR 0049 decision 17) plus about 1.5 GB of host and workers stays under 7 GB; EP3 (4 vCPU, 14 GB) runs 4: 4 × 2,304 MB plus about 2.5 GB.
5. **Plans per environment** (`infra/env/<env>.json`): `ingestion.sku` `EP1` (`dev`), `EP2` (`prod`), `EP2` (`load`); `ingestion.alwaysReady` (integer 1 to 20) 1, 1 and 2; `ingestion.maxBurst` (integer 1 to 100, at most the region's Linux maximum) 2, 4 and 4. The plan sets `sku.capacity` 1, `maximumElasticWorkerCount` = `ingestion.maxBurst` and `elasticScaleEnabled: true`; the function app sets `minimumElasticInstanceCount` = `ingestion.alwaysReady`, `preWarmedInstanceCount` 1, `functionAppScaleLimit` = `ingestion.maxBurst`, `functionsRuntimeScaleMonitoringEnabled: true` (its storage is private) and `WEBSITES_CONTAINER_START_TIME_LIMIT` 900, because a new instance loads the pinned layout and table models at its first start.
6. **The global limit.** `ingestion.max_parallel_items` (ops_config, default 1, range 1 to 16) is the real cap: the dispatcher's claim counts `running` items across all runs and instances (ADR 0034). The Durable settings are per-instance ceilings above it.
7. **Sizing rule.** Parallelism `p` needs `ceil(p / worker processes per instance)` instances, `ceil(p / 2)` on EP2. Because scaling beyond the minimum is best effort, `ingestion.alwaysReady` is set to that count and `ingestion.maxBurst` is at least that count. With `p` at most twice the always-ready EP2 instances, `p` item activities never fill the `4 × instances` activity slots, so the dispatcher's own activities always find one.
8. **Raising parallelism** (runbook R18 and the infrastructure procedure for ingestion parallelism). Before raising `p` above 2 in `prod`: check the ingestion deployments' tokens per minute (`embed-ingest-te3-large-v1` has 250 units, 250,000 tokens per minute, in `prod`; each running document draws at most 2 embedding requests of up to 60,000 tokens and 4 summary calls at once) and Document Intelligence's 15 analyze transactions per second; in one pull request raise `ingestion.alwaysReady` in `infra/env/prod.json` to `ceil(p / 2)` (and `ingestion.maxBurst` when it is lower) and deploy it; then run `docchat-ops config set` for `ingestion.max_parallel_items`. Lower `p` the same way when `docchat.ingestion.throttled` rises (alert A21).
9. **Measurement.** Every parse records its peak resident memory: span attribute `docchat.ingestion.parse_peak_rss_mb` on `docchat.ingestion.parse` and histogram `docchat.ingestion.parse.peak_rss` (MB, `parse_command`), charted in the `capacity` workbook. A part whose processes, summed over the child's UID, exceed the limit are ended and fail the item with `parse_memory_exceeded` (not retryable).
10. **Proof.** Load scenario S4 in `load` (2 always-ready EP2 instances): 200 documents at `p` = 1, then 200 at `p` = 4; both sets end with every item succeeded, the second takes at most half as long as the first, and throttled calls (the model stub throttles every 20th ingestion request) are retried without failing a document.

## Alternatives considered

- **More threads per worker** (`PYTHON_THREADPOOL_THREAD_COUNT`). Parsing must not depend on threads, and async activities don't use that pool. Lost.
- **Two parse slots in one worker process on EP2.** The children would use both cores, but the worker's own CPU work (token counting, sanitising, work-file JSON) would stay on one, and one stalled event loop would hold both parses. Lost.
- **The Durable defaults (10 × processors).** One EP2 instance would accept 20 activities; parses beyond its slots would wait inside activity deadlines on that instance while others idle, since accepted activities can't be rebalanced. Lost.
- **One activity slot per parse slot.** The sweep and claim would queue behind 20-minute parses and the watchdog would restart a healthy dispatcher. Lost.
- **Scale-out alone, with one always-ready instance.** Best-effort scaling may give one instance during a large run, halving parallelism with no error. Lost.
- **EP3 in `prod`.** Its always-ready instance bills continuously for capacity that the default `p` = 1 leaves idle; EP2 runs two parses and the map moves to EP3 by parameter. Lost.

## Consequences

- Idle cost is `ingestion.alwaysReady` instances of the plan's SKU; at the defaults `prod` keeps one EP2, which runs `p` = 1 with a parse slot to spare.
- Raising `p` is two changes in order, capacity first: a pull request for always-ready instances, then the ops setting. Raising the setting alone still works, at best-effort parallelism.
- The SKU map assumes a 2,048 MB parse limit. A measured peak near the limit means raising `DOCCHAT_INGESTION_PARSE_MEMORY_LIMIT_MB`, and then keeping worker processes × (limit + 256 MB) plus the host's share within the SKU's memory; no automated check enforces that sum.
- A higher `p` multiplies model and layout traffic; the separate ingestion deployments keep that load off chat's quota, and throttling shows on `docchat.ingestion.throttled` before it fails a document.
- Obligations: the SKU map and the three environment parameters in Bicep; the deployed test that compares each site's app settings with the settings catalogue; load scenario S4; runbook R18's step for raising `p`; the `capacity` workbook tile for parse memory.

## References

- Azure Functions Premium plan (SKUs, always-ready instances, burst): https://learn.microsoft.com/en-us/azure/azure-functions/functions-premium-plan (checked 2026-10-14)
- Azure Functions hosting limits: https://learn.microsoft.com/en-us/azure/azure-functions/functions-scale (checked 2026-10-14)
- `FUNCTIONS_WORKER_PROCESS_COUNT`: https://learn.microsoft.com/en-us/azure/azure-functions/functions-app-settings (checked 2026-10-14)
- Python scale and performance: https://learn.microsoft.com/en-us/azure/azure-functions/python-scale-performance-reference (checked 2026-10-14)
- Durable Functions performance and scale: https://learn.microsoft.com/en-us/azure/azure-functions/durable/durable-functions-perf-and-scale and https://learn.microsoft.com/en-us/azure/durable-task/durable-functions/durable-functions-perf-and-scale (checked 2026-10-14)
- Durable Functions `host.json` settings: https://learn.microsoft.com/en-us/azure/durable-task/durable-functions/durable-functions-host-json-settings (checked 2026-10-14)
- App settings with a period on Linux Elastic Premium: https://learn.microsoft.com/en-us/azure/azure-functions/configure-monitoring (checked 2026-10-14)
- App Service container start limit: https://learn.microsoft.com/en-us/azure/app-service/reference-app-settings (checked 2026-10-14)
- Document Intelligence service limits: https://learn.microsoft.com/en-us/azure/ai-services/document-intelligence/service-limits?view=doc-intel-4.0.0 (checked 2026-10-14)
