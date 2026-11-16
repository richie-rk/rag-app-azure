# ADR 0151: Dependency capacity in the load test

- **Status:** Accepted
- **Date:** 2026-10-21
- **Decided by:** Architect
- **Specified in:** 84-testing.md, 83-infrastructure.md, 81-config.md, 86-operations.md
- **Related ADRs:** 0030, 0062, 0073, 0112, 0126, 0134, 0135, 0138

## Context

The scale-out scenario S3 (ADR 0134) must push the API's average CPU above 70% for 5 minutes on one P1v3 instance, while every request succeeds. Every chat turn also calls dependencies that the API's autoscale can't help: Cosmos DB (the `conversations`, `memories` and `usage` containers) and the real Prompt Shields account. At `prod`'s maxima (4,000, 1,000 and 1,000 RU/s) and about 300 RU per completed turn, Cosmos DB throttles above about 13 turns per second, and the chat path gives up on a throttled call after 3 attempts and 5 s (ADR 0073), which would fail S3-e for a reason unrelated to autoscale. Prompt Shields' S0 limit can't be raised for a resource created per run. The verdicts read after S3 also need telemetry that still flows: a full S1-to-S4 run sends about 6.5 GB to the workspace, more than a 5 GB daily cap admits.

Vendor facts: Prompt Shields on S0 accepts 1,000 requests per 10 seconds (100 per second); a Cosmos DB autoscale container scales between 10% and 100% of its maximum `Tmax`, instantly within that range, billed per hour at the highest throughput reached, and each physical partition serves at most 10,000 RU/s, so a container's partitions are laid out for the maximum it was created with; `CDBDataPlaneRequests` records each data-plane request's `RequestCharge` and `StatusCode`; a workspace that reaches its daily cap writes an event that `_LogOperation | where Category =~ "Ingestion" | where Detail contains "OverQuota"` returns, and Log Analytics Reader may run that query.

## Decision

1. **S3's maximum rate.** R_max = 60 turns per second.
2. **Cosmos DB in `load`.** `cosmos.maxThroughput` in `infra/env/load.json` is `{conversations: 30000, memories: 30000, usage: 30000}` RU/s, which is R_max × 400 RU per completed turn × 1.25: 400 RU is the most per completed turn that the conversation load test accepts for the conversation containers and S1-h accepts for each container, and 1.25 is the headroom. The containers are created at that maximum, so their physical partitions exist before S1 and no scale-up waits for a partition split. `prod` keeps its own maxima (4,000, 1,000 and 1,000 RU/s), which S1-h checks.
3. **No daily cap in `load`.** `monitoring.dailyCapGb` is −1: about 1.6 GB for S1, S2, the S3 calibration, the seed, the alert probe and S4 (about 11,300 chat turns at about 90 KB each, from about 50 spans of about 1.2 KB, about 20 `CDBDataPlaneRequests` rows of about 1 KB and one log record; about 37,000 cheap requests at about 3 KB; S4's 400 documents at about 1 MB each of spans and storage logs; platform metrics), plus about 4.9 GB for S3's main phase at R_max (60 turns per second × 900 s × 90 KB), about 6.5 GB in all. The environment lives at most 24 hours (ADR 0126), so the run's own volume bounds the cost.
4. **S3's rate from measured demand.** S3 starts on one instance. After the 4-minute calibration at 5 turns per second, `docchat-load s3-rate` reads, over calibration minutes 2 to 4: C5, the mean 1-minute plan `CpuPercentage`; s, the mean Prompt Shields calls per turn (`docchat.calls` on the span `docchat.chat.screen`); ru[c], each container's request charge per completed turn (S1-h's query); and T[c], each container's provisioned autoscale maximum (`az cosmosdb sql container throughput show … --query resource.autoscaleSettings.maxThroughput`). It sets R = min(R_cpu, R_ps, R_db, 60) turns per second, with R_cpu = ceil(5 × 85 ÷ C5) (the workflow input `rate` replaces R_cpu when given), R_ps = floor(80 ÷ s) (100 Prompt Shields requests per second with 25% headroom) and R_db = the smallest floor(T[c] ÷ (1.25 × ru[c])); every value goes into the verdict's `measurements`. R below 5 ends S3 before its main phase as `inconclusive` with cause `dependency_bound`.
5. **When a dependency bound stops CPU short of 70%.** A main phase that fails only S3-a or S3-b with every 1-minute CPU average below 70 is `inconclusive`: cause `calibration_miss` when R = R_cpu (re-run with `rate` = ceil(1.25 × R), still capped by R_ps, R_db and 60), cause `dependency_bound` when R < R_cpu. A `dependency_bound` run is repeated with the stub's answer length four times the last value (`answer_tokens` 80, 320, 1,280, then 4,000; each question carries `[[stub:tokens=<L>]] [[stub:delay_ms=<d>]]` with d = max(1, floor(800 ÷ L)) ms), which raises the API's CPU per turn while each turn's Prompt Shields and Cosmos DB calls stay the same; a `dependency_bound` verdict at 4,000 goes to the product owner with its measurements, because only a higher dependency limit or a smaller per-turn demand can change it (runbook R11 step 12).
6. **`C-db`, in the S1 to S3 verdicts.** No chat-path Cosmos DB request stayed throttled after the SDK's retries: over the scenario window `docchat.api.problems` with `code` `dependency_unavailable`, `docchat.chat.turns` with `error_code` `conversation_unavailable` or `turn_save_failed`, and `docchat.usage.record_failures` sum to 0. The verdict also reports `CDBDataPlaneRequests` rows with `StatusCode` 429 per container (throttling the SDK absorbed); when `C-db` fails and that count is above 0, the verdict names the cause `dependency_capacity`, and R11 compares `ruPerTurn` × R with the containers' maxima and raises `load`'s `cosmos.maxThroughput` by pull request before the next run.
7. **`C-cap`, in every verdict, the alert probe's included.** The workspace never reached a daily cap from the run's start to the verdict: `_LogOperation | where Category =~ "Ingestion" | where Detail contains "OverQuota"` returns no row over that span.
8. **S1-h, the `prod` check.** For each container, S1's charge per completed turn (from `CDBDataPlaneRequests`) is at most 400 RU, and its highest 1-minute consumption (RU ÷ 60 s) is at most that container's `prod` maximum in `infra/env/prod.json` at the release commit, so `prod`'s sizing carries 100 streams; the verdict records `ruPerTurn` and `throttled` per container, and the latter is the conversation load test's 429 count.

## Alternatives considered

- **`load` at `prod`'s Cosmos DB maxima.** Rejected: S3 would throttle at about 13 turns per second and fail on Cosmos DB, not on the autoscale behaviour it exists to prove.
- **Driving S3 at whatever rate puts CPU above 70%, ignoring dependency limits.** Rejected: the real Prompt Shields account refuses more than 100 requests per second and can't be raised for a per-run resource, so failures would measure the screening limit.
- **Stubbing Prompt Shields and Cosmos DB in `load`.** Rejected: the load test runs every real service except the model, so the dependency costs measured per turn are the ones production pays.
- **Raising the containers' maxima during the run.** Rejected: a higher maximum than the partitions were laid out for needs a split, which takes time the scenario can't wait for; creating the containers at 30,000 RU/s avoids it.
- **Keeping a 5 GB daily cap in `load`.** Rejected: a full run sends about 6.5 GB, and once the cap is reached telemetry stops, so the verdict queries would read silence as success; `C-cap` proves it never happened.
- **Raising CPU per turn with an artificial busy loop.** Rejected: it would load the API with work no user causes; a longer streamed answer raises the API's real per-turn work (more deltas encoded, validated and written) at the same dependency demand.

## Consequences

- S3 measures the API's scale-out, and a dependency limit shows up as a named `inconclusive` cause with its measurements instead of a failure that looks like an autoscale defect.
- `load` costs more while it lives: each of the three containers idles at 10% of 30,000 RU/s (3,000 RU/s) and is billed per hour at the highest throughput reached, for at most 24 hours per run.
- `prod`'s Cosmos DB maxima are checked against measured demand on every release run (S1-h), so a change that raises the charge per turn fails before it reaches `prod`.
- Obligations: `infra/env/load.json` holds `cosmos.maxThroughput` and `monitoring.dailyCapGb`; R11 step 12 handles `calibration_miss`, `dependency_bound` and `dependency_capacity`.
- Tests (84-testing.md): `docchat-load s3-rate` sets R to each of R_cpu, R_ps, R_db and 60 when that bound is the smallest, an input `rate` replaces R_cpu only, and R below 5 ends S3 as `dependency_bound`; `C-db` fails on a `dependency_unavailable` problem or a `conversation_unavailable` or `turn_save_failed` turn and names `dependency_capacity` when 429s were recorded; `C-cap` fails on an `OverQuota` row; S1-h fails above 400 RU per turn or above the `prod` maximum; no chat-path request is throttled after the SDK's retries during the 100-stream test.

## References

- Content Safety Prompt Shields query rates: https://learn.microsoft.com/en-us/azure/ai-services/content-safety/overview (checked 2026-10-14)
- Cosmos DB autoscale throughput and billing: https://learn.microsoft.com/en-us/azure/cosmos-db/provision-throughput-autoscale (checked 2026-10-21)
- Cosmos DB service limits (10,000 RU/s per physical partition): https://learn.microsoft.com/en-us/azure/cosmos-db/concepts-limits (checked 2026-10-14)
- Log Analytics daily cap and the `OverQuota` event: https://learn.microsoft.com/en-us/azure/azure-monitor/logs/daily-cap (checked 2026-10-15)
