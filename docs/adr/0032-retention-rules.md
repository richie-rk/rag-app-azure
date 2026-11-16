# ADR 0032: Retention rules

- **Status:** Accepted
- **Date:** 2026-10-18; amended 2026-11-06 (a runbook citation matched to its title in the runbook index)
- **Decided by:** Product owner and architect
- **Specified in:** 12-chat-pipeline.md, 13-conversations-and-memory.md, 14-ingestion.md, 80-data-model.md, 81-config.md, 83-infrastructure.md, 84-testing.md, 85-observability.md, 86-operations.md
- **Related ADRs:** 0029, 0030, 0031, 0033, 0034, 0039, 0045, 0052, 0058, 0074, 0096, 0107, 0143, 0148

## Context

Every store needs one stated lifetime rule, because data kept without a rule grows without limit and keeps whatever it ever held. Finished orchestration instances hold inputs, outputs and failure records; kept for ever, the task hub grows without bound.

- **Conversations are kept indefinitely by product decision.** Retention for conversations and memory is a parameter, the container's default TTL, and it is off by default. User-initiated session deletion is out of scope for the first version; a retention period is configuration.
- **Different data has different useful lives.** Rate windows and leases matter for minutes; quota counters for a day or a month; run history for as long as admins investigate ingestion; orchestration history for incident investigation; scanner results for incident review; audit rows and the document records behind citation `410` answers for good.
- **Expiry should need no privileged code where the platform can expire data itself.** A purge job over conversations would need a cross-partition query and an identity with delete rights on every user's data.

Vendor facts that bound the choice: with a container's `defaultTtl` missing or null, items never expire and an item's `ttl` has no effect; `-1` means no default expiry, but an item whose `ttl` is `n` expires `n` seconds after its last modification; a positive `defaultTtl` `n` expires every item `n` seconds after its last modification; expired items disappear from query results before the background delete, which is delayed when request units are short; serverless TTL deletions are billed at the delete rate; the largest TTL is 2,147,483,647 seconds. A continuous-backup restore restores the TTL settings too, which deletes items already past their TTL, unless `restoreWithTtlDisabled` is set. The GA Durable client's `purge_instance_history_by(created_time_from, created_time_to, runtime_status)` deletes the history of every instance that matches. With blob soft delete on, a lifecycle delete leaves the blob soft-deleted for the retention period (ADR 0031).

## Decision

Every store has one stated rule. Values marked *setting* change through configuration (an `ops_config` row or the environment file) without a code change; the rest are constants in code or Bicep.

**Cosmos DB (database `docchat`).**

- `conversations` and `memories`: `defaultTtl` comes from the environment-file fields `conversationRetentionDays` and `memoryRetentionDays` (`environmentConfig` in `infra/env/<env>.json`, read by Bicep; *setting*, integer 0 to 3,650, default 0). Value 0 sets `defaultTtl` to `-1`; a value `N` sets `N × 86,400` seconds. No document in either container ever carries its own `ttl`, so `-1` means nothing expires, and with `N` set each item expires `N` days after its own last modification.
- `usage`: `defaultTtl` `-1`; every document carries its own `ttl`: day counter `usage_day` 3,456,000 s (40 days after its last write); month counter `usage_month` 34,560,000 s (400 days); `rate_window`, `download_rate_window` and `write_rate_window` 180 s; `stream_lease` `chat.max_turn_seconds` + 30 s (210 s by default); `download_lease` 300 s.

**Ingestion state.** `ingestion_maintenance` (timer `0 40 2 * * *`, 02:40 UTC daily) runs four steps, each separate: a failed step writes one warning log record `maintenance_failed` (`step`), the next step still runs, and the next day retries. Step 3 writes the day's scanner canary (ADR 0039); the other three are retention:

- Step 1, orchestration history: `purge_instance_history_by(created_time_from=datetime(2020, 1, 1, tzinfo=UTC), created_time_to=now − ingestion.durable_history_retention_days, runtime_status=[Completed, Failed, Terminated])`; *setting* default 30 days, range 7 to 365. The running dispatcher is never in a terminal state, so it is never purged.
- Step 2, runs: repeat until no row is affected, `DELETE TOP (500)` the items of runs in `completed`, `completed_with_errors` or `failed` whose `completed_at` is older than `ingestion.run_retention_days` (*setting* default 180, range 30 to 3,650), then the same for the runs, a run only once it has no items left and no physical index names it as its build run. `physical_indexes.build_run_id` is cleared when an index leaves `building`, but the `building` physical index left by a logical index whose creation failed keeps its build run until a re-ingest retires it, and that run is skipped so that the delete never fails on the foreign key; `chunk_sets.build_item_id` has no foreign key and blocks nothing.
- Step 4, scanner canaries, whatever the scanning setting: first delete the blob of every open canary (`result` null) whose `written_at` is more than 90 days old (an absent blob counts as deleted), then repeat `DELETE TOP (500) FROM app.scan_canaries WHERE written_at < DATEADD(day, -90, SYSUTCDATETIME())`. A failed blob delete ends the step before any row is deleted, so no canary blob outlives its row. 90 days is a constant, not a setting.

The sweep removes `uploading` versions whose `reserved_at` is older than `ingestion.stale_upload_hours` (*setting* default 24, range 1 to 168), up to 200 per wake, with any blob at their path, and deletes a document row left with no version.

**Every other SQL table** (schema `app`):

| Table | Lifetime |
|---|---|
| `users` | until `docchat-ops user delete` (ADR 0033) |
| `projects` | indefinite: archived and restored, never deleted |
| `project_members` | until removed, or until the user is deleted |
| `project_indexes` | until unmapped |
| `user_index_overrides` | until cleared or the user is deleted; expired rows are ignored, never deleted |
| `ops_config` | one row per catalogue key, removed by the contract migration that retires the key |
| `audit_events` | indefinite (append-only) |
| `ingestion_methods`, `search_indexes`, `physical_indexes` | indefinite; deleted indexes stay as history |
| `documents`, `document_versions`, `chunk_sets` | indefinite: they back citation `410` answers and a deleted name's return under its document ID |
| `served_chunk_sets` | until the next Flip, the document's deletion or the physical index's deletion |
| `ingestion_runs`, `ingestion_run_items` | 180 days after the run completes |
| `ingestion_dispatcher_state` | one row |
| `scan_canaries` | 90 days after `written_at` |

**Blobs** (ADR 0031): a source and its renditions until clean-up purges them, then 14 days soft-deleted; work files deleted by clean-up or 7 days after their last modification, then 14 days soft-deleted; canary blobs until their result is recorded, at most 90 days; blocks staged by an upload that never committed, one week after the last staged block. The host account's task-hub blobs follow step 1, with 7-day blob and container soft delete. **Search:** a retired chunk set's chunks are deleted by key at clean-up; a physical index is deleted no sooner than 10 minutes after the repoint (ADR 0045) or with its logical index (ADR 0052).

**Telemetry and backups** (recorded here so every lifetime is in one place; owned by ADR 0107 and the infrastructure ADRs): Log Analytics keeps 30 days in `dev` and `load` and 90 days in `prod`; in `prod` the tables `CDBDataPlaneRequests`, `SQLSecurityAuditEvents`, `AZKVAuditLogs`, `StorageBlobLogs` and `AppServiceAuditLogs` keep 90 days interactive and 365 days in total; `log-docchat-security` keeps 365 days. Cosmos DB continuous backup is `Continuous30Days` in `prod` and `Continuous7Days` in `dev` and `load`; SQL point-in-time retention is 14 days in `prod` and 7 days in `dev` and `load`, with no long-term retention.

## Alternatives considered

- **A purge job for conversations and memory:** needs a cross-partition query by age, its own schedule and an identity with delete rights on every user's partition; container TTL needs no code and no identity, and expired items vanish from queries at once.
- **Per-type TTLs inside `conversations` (traces shorter than turns):** conversations are kept as one record by product decision; one parameter per container keeps a single rule that an operator can audit.
- **Whole-session expiry (delete a session and its turns together when its last activity is older than `N` days):** needs a job and cross-partition reads; per-item TTL is the platform's rule, and its effect on summaries is stated below.
- **Keeping orchestration history for ever:** the task hub grows without bound. **Purging after 7 days:** loses evidence for problems reported weeks later; 30 days covers incident investigation, and the range lets an operator change it.
- **Keeping runs for ever, or 90 days:** the Ingestion runs screen would page through ever-growing tables; 90 days loses the previous quarter. 180 days keeps two quarters of run history.
- **Deleting document, version and chunk-set rows with their blobs:** citations to a deleted document would answer `404` where the product promises `410` "this document has changed", and a deleted name couldn't return under its document ID.
- **A setting for canary retention:** the upload gate reads only the last 48 hours; 90 days keeps a quarter of the scanner's results for incident review, and a setting would add a value nobody needs to change.
- **Usage counters kept for ever, or removed by a job:** the quota gate reads only the current day's and month's counters; per-item TTL removes old ones with no code, and 40 and 400 days keep the last month of daily and the last year of monthly usage readable for a user's export and support questions.

## Consequences

- With retention on, a session document changes at every turn and summary rebuild, so an active session outlives its oldest turns, and its rolling summary can carry content of turns older than `N` days while the session stays active. The summary rebuild recomputes its turn counters when retention has removed turns.
- Turning retention on deletes, in the background, every item whose last change is older than the period; only a point-in-time restore inside the backup window can bring it back, so a `prod` change is announced 14 days ahead (runbook "Change conversation or memory retention").
- A restore keeps TTL enabled (never `restoreWithTtlDisabled`), so restored items past the retention period expire again; the deletion register is re-applied after any restore (ADR 0033).
- TTL deletes consume request units: the background delete lags when a container is busy, and on `dev`'s serverless account each delete is billed.
- `audit_events` grows for ever, one small row per admin, operator and security action; its time index keeps reads bounded.
- A run older than 180 days disappears from the Ingestion runs screen; its document, version and chunk-set rows stay.
- A failed maintenance step is visible as its `maintenance_failed` warning (`step`) and runs again the next day; nothing is lost by a missed day, because each step deletes everything past its age.
- Retention changes are operator actions: `ingestion.run_retention_days` and `ingestion.durable_history_retention_days` through `docchat-ops config set` (applied at the next 02:40 UTC run), the container fields through a reviewed pull request and a deploy.
- Tests: integration, the purge removes only terminal orchestration instances older than 30 days and runs with their items older than 180 days; integration as the `docchat_ingestion` role, canary retention deletes a 91-day row and keeps an 89-day one, deletes an open canary's blob before its row, and deletes nothing when that blob delete fails; integration, a container with a 5-second `defaultTtl` expires items 5 seconds after their last change and `-1` expires nothing.

## References

- Time to live in Cosmos DB: https://learn.microsoft.com/en-us/azure/cosmos-db/time-to-live (checked 2026-10-14)
- Container resource (`defaultTtl`): https://learn.microsoft.com/en-us/azure/templates/microsoft.documentdb/2026-03-15/databaseaccounts/sqldatabases/containers (checked 2026-10-14)
- Continuous backup and point-in-time restore: https://learn.microsoft.com/en-us/azure/cosmos-db/continuous-backup-restore-introduction (checked 2026-10-14)
- Durable orchestration client, `purge_instance_history_by`: https://learn.microsoft.com/en-us/python/api/azure-functions-durable/azure.durable_functions.durableorchestrationclient?view=azure-python (checked 2026-10-18)
- Eternal orchestrations and history growth: https://learn.microsoft.com/en-us/azure/durable-task/common/durable-task-eternal-orchestrations (checked 2026-10-14)
- Lifecycle management and soft delete: https://learn.microsoft.com/en-us/azure/storage/blobs/lifecycle-management-overview (checked 2026-10-14)
