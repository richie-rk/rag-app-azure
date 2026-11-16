# ADR 0028: Ops config as typed, seeded rows

- **Status:** Accepted
- **Date:** 2026-10-18
- **Decided by:** Product owner and architect
- **Specified in:** 80-data-model.md, 81-config.md, 82-security.md, 12-chat-pipeline.md, 14-ingestion.md, 84-testing.md, 85-observability.md, 86-operations.md
- **Related ADRs:** 0008, 0020, 0024, 0027, 0029, 0034, 0036, 0139, 0147

## Context

Runtime operational values (ingestion parallelism, which defaults to 1, rate limits, quotas, retrieval and fallback thresholds, summary and memory caps, feature toggles) are operator settings kept in SQL `ops_config` as typed rows. Operators change them through an audited command, never through the admin UI, and processes cache them for at most 60 seconds. Every setting is a Pydantic model.

Forces:

- **A setting is data, never code.** A setting that is evaluated as code lets whoever can write the setting run code inside the service, and an untyped string such as `'false'` must never switch a feature on. Each key must exist once, under one owner, with one type and one range.
- **Twelve areas, four owners, one table:** the identity section's `api`; the chat pipeline's `chat`, `retrieval`, `followups`, `ratelimit`, `quota`, `pricing`; the conversation section's `conversations`, `summary`, `memory`, `feedback`; ingestion's `ingestion`.
- **Change without a redeploy.** A value must change without a deployment, and every API worker and the ingestion dispatcher must see it soon.
- **No periodic SQL query.** A paused `dev` database must stay paused (ADR 0036), so reading configuration may add no timer-driven query.
- **A bad value must never take down a running process and never silently change behaviour.**
- **Keys arrive before code.** An expand revision ships a key before the code that reads it is deployed (ADR 0027), and during a deployment the previous release runs beside rows it doesn't know.
- Vendor facts: a `rowversion` column changes on every insert or update, and the optimistic-concurrency pattern is `UPDATE … WHERE key = @k AND row_version = @rv` followed by a check of the affected row count (checked 2026-10-14); a serverless database pauses only after zero sessions for the whole auto-pause delay (checked 2026-10-14).

## Decision

1. **The table.** `app.ops_config`, one row per catalogue key:

| Column | Type | Null | Default | Rule |
|---|---|---|---|---|
| `config_key` | `NVARCHAR(64)` | no | | `PK_ops_config`; `CK_ops_config_key`: `config_key COLLATE Latin1_General_BIN2 NOT LIKE N'%[^a-z0-9_.]%' AND CHARINDEX(N'.', config_key) > 1 AND RIGHT(config_key, 1) <> N'.'` (dotted lower case `<area>.<name>`) |
| `value_json` | `NVARCHAR(MAX)` | no | | `CK_ops_config_value_json`: `ISJSON(value_json) = 1`; shape `{"value": <typed value>}` |
| `default_json` | `NVARCHAR(MAX)` | no | | `CK_ops_config_default_json`: `ISJSON(default_json) = 1`; same shape; written only by revisions |
| `change_reason` | `NVARCHAR(500)` | yes | | `NULL` while seeded; after an operator change the command's `--reason`, 10 to 500 characters (`CK_ops_config_reason`: `change_reason IS NULL OR LEN(change_reason) >= 10`) |
| `updated_at` | `DATETIME2(3)` | no | `SYSUTCDATETIME()` | |
| `updated_by` | `UNIQUEIDENTIFIER` | yes | | `NULL` while seeded; the operator's `oid` after a change |
| `row_version` | `ROWVERSION` | no | | optimistic concurrency of `config set` and `config reset` |

   Wrapping every value in `{"value": …}` keeps `ISJSON` meaningful for scalars. Grants: `docchat_api` and `docchat_ingestion` `SELECT`; `docchat_config_operator` `SELECT` and `UPDATE`, never `INSERT` or `DELETE`; `docchat_user_data_operator` `SELECT` (the export lists rows whose `updated_by` is the user). Only revisions, run by the deploy identity, insert or delete rows (ADR 0027).
2. **A catalogue of keys.** Each key belongs to the area named by the text before its first dot. The catalogue is compiled from every owning section's settings table (key, area, type, default, allowed range, owner, readers, purpose) and printed in 81-config.md; the identity section owns one key, `api.write_requests_per_minute` (integer, default 120, 10 to 1,200).
3. **Seeded by the revision that ships the key.** It inserts the row with `value_json` = `default_json` = `{"value": <default>}`. A revision that changes a default updates `default_json`, and `value_json` only where it still equals the old default, so an operator's choice survives. A contract revision deletes a retired key.
4. **Typed by the owner's area model.** `docchat_core.opsconfig.OpsConfig` is a root model with one sub-model per area: `ApiOps` (identity); `ChatOps`, `RetrievalOps`, `FollowupsOps`, `RateLimitOps`, `QuotaOps`, `PricingOps` (chat pipeline); `ConversationsOps`, `SummaryOps`, `MemoryOps`, `FeedbackOps` (conversations and memory); `IngestionOpsConfig` in `docchat_core.ingestion.settings` (ingestion). Every field is aliased to its full dotted key, carries its range and has no code default, because the database is the source; sub-models use `extra="forbid"` and hold their cross-field rules (among them `retrieval.keep_count` ≤ `retrieval.candidate_count`, `quota.tokens_per_month` ≥ `quota.tokens_per_day`, `summary.keep_recent_tokens` below `summary.trigger_tokens`). Pydantic parses JSON values as typed data; nothing is ever evaluated.
5. **Start-up.** Each process registers the areas it reads. `OpsConfigStore` reads every row with one statement (`SELECT config_key, value_json FROM app.ops_config`; a few hundred rows), groups rows by area and validates each registered area from the `value` members of its keys. Before validating, it drops rows of unregistered areas and keys the area's model doesn't declare, because an expand revision ships a key before the code that reads it; the model's `extra="forbid"` still refuses any undeclared field built in code. A missing key or an invalid value in a registered area raises, and the process exits non-zero (App Service restarts the container); the log names the area and key, never the value. In ingestion, the dispatcher reads its snapshot at each wake through `load_dispatch_settings` and carries its last valid snapshot across `continue_as_new`; a dispatcher with no valid snapshot sweeps and claims nothing, reports `configValid: false` in its custom status and tries again after 60 seconds (ADR 0036).
6. **One snapshot per unit of work, refreshed lazily.** `store.snapshot()` returns the immutable root model while it is younger than 60 seconds; the first access after that refreshes it under a lock, and concurrent callers wait for that one refresh. Callers take one snapshot per request: one per turn on the chat path, one per dispatcher wake, whose values each item receives in its sub-orchestration input. A refresh replaces each registered area only when it validates; an area that fails keeps its last valid sub-model, increments `docchat.config.invalid` (`area`, `key`) and emits `ops_config_invalid` (custom event and error log record); a refresh that can't reach SQL keeps the snapshot, logs `ops_config_unreachable` (warning), increments `docchat.opsconfig.refresh_failures` (`reason` `unreachable`) and allows the next attempt no sooner than 15 seconds later. Each refresh is one span `docchat.opsconfig.refresh` (`docchat.opsconfig.outcome` `ok`, `partial` or `unreachable`; `docchat.opsconfig.invalid_areas`). No timer refreshes the snapshot.
7. **Changed only through `docchat-ops config`.** The operator runs `uv run docchat-ops --env <env> config show|set|reset` (ADR 0139) under `AzureCliCredential`, from an address in the environment's operator allowlist, as a just-in-time member of `sg-docchat-<env>-config-operators` (contained user in `docchat_config_operator`), granted for at most 4 hours through PIM for Groups or `docchat-ops jit grant --level config` by a group owner who isn't the operator (ADR 0024).
   - `config show [--key <key>]` prints each key's value, default, last change time and actor ID; it reads no personal data and takes no `--ticket` or `--reason`.
   - `config set --key <key> --value '<JSON>' --ticket <id> --reason '<10 to 500 characters>'` refuses a key outside the catalogue; validates the value with the area's model against the area's current values, cross-field rules included; prints old and new values; in `prod` asks for the typed environment name; then, in one transaction, updates the row `WHERE config_key = :key AND row_version = :rv`, storing `change_reason` and `updated_by`, and inserts `ops_config.set` (`actor_type` `operator`; metadata `area`, `previous`, `new`, `ticket`; ADR 0029). Zero rows updated means someone else changed it: exit 6, nothing written. Output says the value is live within 60 seconds.
   - `config reset --key <key> --ticket <id> --reason '<…>'` sets `value_json` to `default_json` under the same checks and inserts `ops_config.reset` (metadata `area`, `previous`, `ticket`).
   - Exit codes: 0 done; 2 usage error or validation failure (unknown key, out-of-range value, a reason outside 10 to 500 characters), nothing written; 3 access denied (no sign-in, or no just-in-time membership, so SQL refuses the login); 6 conflict; 7 database unreachable; 1 anything else, with the run's correlation ID.
   - No API operation and no admin screen writes the table.
8. **One exception, in `load` only.** The `load` deploy identity, a standing member of that environment's SQL Entra admin group, applies the load-test values, their turn-cap reset and the ingestion-parallelism changes from the load workflow's `seed-config`, `stream-reset`, `s4-set1-config`, `s4-set2-config` and `s4-reset` jobs, writing `operator` audit rows with its own object ID, because a load run can't wait for a person between scenarios. `docchat-ops` refuses a service-principal caller for `config set` and `config reset` in `dev` and `prod` (exit 3).
9. **Linked checks before a change.** A change to a value in either capacity share's formula is evaluated first with `docchat-ops chat capacity-check --set <key>=<json>`, which writes nothing (ADR 0147); raising `ingestion.max_parallel_items` follows the ingestion runbook's check of the Azure OpenAI and Document Intelligence quotas (ADR 0034).

## Alternatives considered

- **App settings for runtime values.** Rejected: a change would need a deployment and a restart that ends running streams, and changing it would need control-plane write access to the app, outside the audited path. App settings keep the values that need a redeploy (ADR 0008).
- **Editing in the admin UI.** Rejected: operational limits are operator settings, never admin settings, and a UI path would put them within reach of every admin session.
- **Code defaults with database overrides.** Rejected: a missing or misspelled row would silently run on a default, and the value in effect would depend on the release; with the database as the only source, a missing key fails start-up and a revision seeds every default.
- **A value-type column, or one column per key.** Rejected: a type column duplicates what the area model declares and can disagree with it; one column per key turns every new setting into a schema change. One JSON value per row, validated by the owner's model, holds integers, booleans, enums and objects (the price table) alike.
- **A refresh timer.** Rejected: every worker would query SQL every minute, keeping the `dev` database from pausing (ADR 0036), and idle processes would refresh for nobody.
- **Push invalidation** (a queue message or SQL change tracking on update). Rejected: another moving part to secure and test, for operational values that may be up to 60 seconds stale.
- **On an invalid value: fall back to the default, or fail requests.** Rejected: a fallback silently changes behaviour; failing requests turns one typo into an outage. Keeping the last valid area and alerting does neither.
- **Azure App Configuration.** Rejected: another service, private endpoint, identity and audit trail, while SQL already holds the data these values govern and the audit rows that record their changes.

## Consequences

- Settings are typed data that is parsed and range-checked, never evaluated; `false` is `false`, a limit is an integer within its range, and each key exists once under one owner.
- A committed change is live within 60 seconds in every API worker whenever SQL is reachable, and at the dispatcher's next wake in ingestion (within 60 seconds while work is outstanding). A turn and an ingestion item each run on one consistent set of values. While SQL is unreachable, or while an area is invalid, processes keep an older snapshot: the one case in which a value in use is older than 60 seconds.
- The command refuses an invalid value before writing. A stored value that is invalid anyway keeps running processes on their last valid area and fires alert A39 (`ops-config-invalid`: any `docchat.config.invalid` in 15 minutes, severity 2, runbook R03); a process that starts meanwhile exits (API) or claims nothing (dispatcher), so a restart, deployment or scale-out fails until an operator corrects the value with `config set` or `config reset`.
- Risk: two concurrent `config set` runs on different keys of one area each validate against the other's old value, and `row_version` guards only its own row, so together they can break a cross-field rule; the outcome is the alert path above, never a running process on an invalid value.
- Retiring a key takes two releases: the release that stops reading it, then the contract revision that deletes its row (ADR 0027).
- Every change has an audit row with its ticket and old and new values, and the row itself carries the latest reason and actor. No standing write access exists: every change needs a just-in-time grant approved by a second person and an allowlisted address.
- The full-table read is the one statement exempt from the query-plan audit; the table stays at a few hundred rows.
- Tests: unit (every area model rejects a missing key, an out-of-range value and its cross-field violations; the loader ignores unknown keys; an invalid refresh keeps the area's last valid snapshot and increments `docchat.config.invalid`; snapshots can't be mutated); integration (start-up fails when a registered area misses a seeded key; a value changed by `docchat-ops config set` is seen by a running process within 60 seconds; rows of an unknown area are ignored; `config set` refuses an out-of-range value with exit 2; of two concurrent sets on one key the second exits 6 and writes nothing; a valid set writes its row and audit row in one transaction, and a forced audit failure rolls the change back; the command refuses a missing `--ticket` or `--reason` and a service-principal caller in `prod`); deployed (a member of `sg-docchat-dev-config-operators` can run `config show` and `config set`, and a non-member exits 3); the test fixture re-inserts the seed rows exactly as the revisions wrote them after every test.

## References

- `rowversion` and the optimistic-concurrency pattern: https://learn.microsoft.com/en-us/sql/t-sql/data-types/rowversion-transact-sql (checked 2026-10-14)
- Azure SQL serverless auto-pause conditions: https://learn.microsoft.com/en-us/azure/azure-sql/database/serverless-tier-auto-pause-resume?view=azuresql (checked 2026-10-14)
- Pydantic on PyPI: https://pypi.org/project/pydantic/ (checked 2026-10-14)
