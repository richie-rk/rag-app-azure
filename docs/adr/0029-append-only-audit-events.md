# ADR 0029: Append-only audit events

- **Status:** Accepted
- **Date:** 2026-10-21
- **Decided by:** Architect
- **Specified in:** 80-data-model.md, 82-security.md, 13-conversations-and-memory.md, 14-ingestion.md, 85-observability.md, 86-operations.md, 84-testing.md
- **Related ADRs:** 0014, 0024, 0025, 0027, 0033, 0077, 0108, 0115

## Context

The product keeps an append-only record of admin and security actions: who acted, what they did, on which target, when, and metadata without content. Operators change ops config and per-user index overrides only through audited scripts, every memory change writes an audit event that never holds the fact text, and no log or trace carries content. Changes happen in two stores: SQL (projects, members, mappings, indexes, documents, runs, ops config, overrides) and Cosmos DB (memory). Three kinds of actor make them: a signed-in user through the API, a person running `docchat-ops` under just-in-time access, and an app acting on its own (the ingestion app failing a version for malware, the API extracting memory facts).

When an unexpected project, membership or index change appears, an investigator must be able to name the caller and scope everything that caller did. That needs an identity on every change taken from a validated credential, a durable record that the change can't exist without, and a record that a compromised app identity can't alter.

Facts that bound the design:

- A SQL change and its audit row can share a transaction; a Cosmos DB change can't, because the two stores have no common transaction.
- SQL table grants decide who may insert, update or delete rows (ADR 0025); the server's Microsoft Entra administrator, here the deploy identity through its group, holds every permission regardless of grants.
- Azure SQL's privileged audit (ADR 0108) records every statement of every principal except the two workload identities, so a row an operator's process skipped or forged is still visible as the statements that process ran.
- Append-only ledger tables in Azure SQL block updates and deletes for every principal, but a ledger table can never be converted back, its older rows can never be deleted, and later schema changes are limited (non-nullable columns can't be added and a column's type can't change).

## Decision

1. **The table.** `app.audit_events`:

   | Column | Type | Rule |
   |---|---|---|
   | `id` | `BIGINT IDENTITY(1,1)` | `PK_audit_events`, clustered |
   | `occurred_at` | `DATETIME2(3)` | default `SYSUTCDATETIME()` |
   | `actor_type` | `NVARCHAR(32)` | `CK_audit_events_actor_type`: `user`, `operator` or `system` |
   | `actor_object_id` | `UNIQUEIDENTIFIER` | not null |
   | `subject_object_id` | `UNIQUEIDENTIFIER` | null; the user whose data or access the action concerns |
   | `action` | `NVARCHAR(64)` | `CK_audit_events_action`: the catalogue below |
   | `target_type` | `NVARCHAR(32)` | `CK_audit_events_target_type`: the catalogue below |
   | `target_id` | `NVARCHAR(128)` | lower-case UUID text, an ops-config key or a queue name; null only for `test_users.rotated` |
   | `project_id` | `UNIQUEIDENTIFIER` | null; no foreign key |
   | `metadata_json` | `NVARCHAR(MAX)` | null, or `ISJSON(metadata_json) = 1` and at most 4,000 characters |
   | `correlation_id` | `NVARCHAR(32)` | null, or exactly 32 lower-case hex characters (the W3C trace ID) |

   Indexes: `IX_audit_events_subject (subject_object_id, occurred_at) WHERE subject_object_id IS NOT NULL`, `IX_audit_events_actor (actor_object_id, occurred_at)`, `IX_audit_events_project (project_id, occurred_at) WHERE project_id IS NOT NULL`, `IX_audit_events_target (target_type, target_id, occurred_at)`. No app operation reads the table.
2. **Append-only by grants.** `docchat_api`, `docchat_ingestion`, `docchat_config_operator` and `docchat_conversation_reader` hold `INSERT` only; `docchat_user_data_operator` holds `SELECT` and `INSERT`, for the user-data export and its own rows. No role holds `UPDATE` or `DELETE`. Rows are kept indefinitely in the first version, and a user's data deletion keeps them (ADR 0033).
3. **Actors.** `user`: a caller through the API, actor = the validated token's `oid`. `operator`: a person running `docchat-ops`, actor = the `oid` of that person's SQL access token. `system`: an app acting on its own, actor = its managed identity's principal ID (app setting `DOCCHAT_CORE_WORKLOAD_PRINCIPAL_ID`). No actor comes from a request field.
4. **Content-free metadata.** Each action has a Pydantic metadata model with `extra="forbid"` in `docchat_core.audit`; `metadata_json` holds IDs, counts, field names, enum values, booleans and timestamps only, never names, e-mail addresses, titles, questions, answers, comments, fact text, file names or free-text reasons. Operator rows carry `ticket`, the `--ticket` value of `docchat-ops` (1 to 64 letters, digits, `-`, `_` or `#`). Ops-config values (`previous`, `new`) are the one kind of value allowed, because they are configuration.
5. **A closed catalogue.** An action is `<area>.<event>`, lower-case snake case with exactly one dot, at most 64 characters; a target type is lower-case snake case, at most 32 characters. `CK_audit_events_action`, `CK_audit_events_target_type` and the `Literal` types of `docchat_core.audit` list exactly these 43 actions:
   - access: `user.provisioned`, `project.created`, `project.updated`, `project.archived`, `project.restored`, `project_member.added`, `project_member.removed`, `project_index.mapped`, `project_index.default_set`, `project_index.unmapped`, `ops_config.set`, `ops_config.reset`, `user_index_override.set`, `user_index_override.cleared`;
   - conversations and memory: `memory.paused`, `memory.resumed`, `memory.fact_updated`, `memory.fact_deleted`, `memory.cleared`, `memory.facts_extracted`, `conversation.read`, `user_data.exported`, `user_data.deleted`, `document.traces_redacted`;
   - ingestion: `index.created`, `index.updated`, `index.deleted`, `index.reingest_started`, `ingestion.reindex_started`, `document.upload_committed`, `document.malware_detected`, `document.source_fetched`, `document.deleted`, `document.reingest_requested`, `ingestion.run_submitted`, `ingestion.run_cancelled`, `ingestion.item_requeued`, `ingestion.item_failed`, `ingestion.rebuild_abandoned`, `ingestion.deployments_changed`, `ingestion.poison_drained`;
   - test operations: `test_users.rotated`, `model_deployments.checked` (the result of `docchat-ops chat model-check` in `dev`);

   and these 18 target types: `user`, `project`, `project_member`, `project_index`, `ops_config`, `user_index_override`, `memory`, `memory_fact`, `session`, `search_index`, `document`, `document_version`, `ingestion_run`, `ingestion_run_item`, `physical_index`, `ingestion_queue`, `test_users`, `model_deployments`. A new action or target type ships with an expand migration that re-creates both constraints with the wider lists, its `Literal` member and its metadata model.
6. **SQL changes: the same transaction.** `docchat_core.audit.write_audit_event(connection, event)` inserts on the caller's connection inside the caller's transaction, with `correlation_id` from the current span. No SQL change exists without its row, and a failed audit insert rolls the change back. API admin operations, the ingestion app (`document.malware_detected`) and every `docchat-ops` command that changes SQL use it. `document.source_fetched` records a read, not a SQL change: `docchat-ops document fetch-source` writes it in a transaction of its own once the file is written and its SHA-256 checked, and a failed insert makes the command remove the file and exit 1, so no fetched file lacks its row (ADR 0152).
7. **Cosmos DB changes: right after the commit.** `docchat_core.audit.write_audit_event_after_commit(event)` is the one path for changes committed outside SQL. It runs the insert as its own unit of work (`retry="none"` in the SQL layer) and retries 3 times, after 0.2, 0.8 and 2 seconds, error 40613 included. When every attempt fails it emits the custom event `audit_write_failed` (`action`, `actorType`, `targetType`, `targetId`, and `sessionId` and `turnId` when the metadata holds them) with one error log record, increments `docchat.audit.write_failures` (dimension `action`), and returns without raising, because the change it records is already committed. Alert A24 `audit-write-failures` (severity 2) fires when that counter is above 0 in 15 minutes.
8. **Reads of conversation data are audited too.** `docchat-ops user read-conversation` inserts `conversation.read` (`turnIds`, at most 50, `ticket`, `level`) after it writes its output; Cosmos DB data-plane request logs record every read independently.
9. **What isn't a row here.** Just-in-time grants and revocations, app-role assignments and group membership changes are recorded by Entra ID's audit log and the Activity Log: the tenant diagnostic setting sends `AuditLogs` to `log-docchat-security` (365 days), and a second setting, `docchat-prod-audit`, also sends them to the `prod` workspace. Telemetry carries the custom event `admin_change` (`action`, `targetType`, `projectId`) for dashboards only; the table is the record.

## Alternatives considered

- **Audit records as telemetry only (custom events in Application Insights).** Rejected: telemetry can't share the change's transaction, is sampled and expires with the workspace's retention, so a change could exist without its record.
- **Write the audit row before the Cosmos DB change.** Rejected: a change that then fails would leave a row recording something that never happened; after the commit, the only gap is a missing row, which the alert makes visible.
- **A transactional outbox in Cosmos DB, relayed to SQL by a change-feed processor.** Rejected: it needs a long-running relay in a stateless API and a second copy of every memory change; the after-commit insert with 3 retries and an alert covers the rare SQL outage.
- **An append-only ledger table.** Rejected: it would also stop the deploy identity from changing rows, but it can never be converted back, no row could ever be deleted if a future rule requires it, and expand migrations couldn't add a non-nullable column; the privileged SQL audit already records every statement of the principals that grants don't confine.
- **Triggers that refuse `UPDATE` and `DELETE`.** Rejected: grants already refuse them to every app and operator role, and the server administrator who isn't bound by grants can drop a trigger.
- **A free-text reason column.** Rejected: free text can carry names and content; the ticket identifier links a row to its approval record.
- **One audit table per area.** Rejected: one table with one closed catalogue gives the user-data export and an investigation one query per actor, subject, project or target.

## Consequences

- Every SQL change made by an admin, an operator or an app is attributable to an object ID, a correlation ID and, for operators, a ticket; a memory change loses its row only if SQL stays unavailable through 3 retries, and then alert A24 names the action and target.
- Operator rows are written by the operator's own `docchat-ops` process, which could skip or forge them; the privileged SQL audit records that process's statements in the security workspace outside the pipeline's reach (ADR 0108), and the deploy identity's rights are bounded by ADR 0115.
- Obligations: every new admin, operator or system action adds its catalogue entry, metadata model and expand migration in the same pull request; 82-security.md lists the catalogue; the export of ADR 0033 reads rows by actor and subject.
- The table grows without bound in the first version; its indexes serve the export and investigations, and a retention rule would be a new ADR.
- Tests: every metadata model rejects unknown keys and free-text values; the two `CHECK` constraints accept every catalogue entry and refuse a value one character away from one; `docchat_api` is denied `SELECT`, `UPDATE` and `DELETE` on the table; every admin change writes its rows in its own transaction and a forced audit-insert failure rolls the change back; a forced SQL failure during `write_audit_event_after_commit` retries 3 times, then emits `audit_write_failed`, increments the counter and returns without raising; a memory edit with SQL unavailable answers `200` with exactly one failure event and one counter increment; every operator change's row carries its `--ticket` value.

## References

- Database-level roles and permissions (the Microsoft Entra administrator holds every permission): https://learn.microsoft.com/en-us/sql/relational-databases/security/authentication-access/database-level-roles?view=azuresqldb-current (checked 2026-10-18)
- Auditing for Azure SQL Database (audit action groups, Log Analytics destination): https://learn.microsoft.com/en-us/azure/azure-sql/database/auditing-overview?view=azuresql (checked 2026-10-14)
- Ledger considerations and limitations (append-only ledger tables): https://learn.microsoft.com/en-us/sql/relational-databases/security/ledger/ledger-limits?view=azuresqldb-current (checked 2026-10-21)
- Transactional batch operations in Azure Cosmos DB (one partition key, no cross-store transaction): https://learn.microsoft.com/en-us/azure/cosmos-db/transactional-batch (checked 2026-10-14)
- Microsoft Entra audit logs routed to Log Analytics: https://learn.microsoft.com/en-us/entra/identity/monitoring-health/howto-integrate-activity-logs-with-azure-monitor-logs (checked 2026-10-21)
