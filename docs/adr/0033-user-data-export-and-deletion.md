# ADR 0033: User data export and deletion

- **Status:** Accepted
- **Date:** 2026-10-18
- **Decided by:** Architect
- **Specified in:** CONTEXT.md, 13-conversations-and-memory.md, 80-data-model.md, 82-security.md, 84-testing.md, 86-operations.md, 90-build-order.md
- **Related ADRs:** 0021, 0024, 0029, 0030, 0032, 0078, 0139, 0149

## Context

A person who leaves the organisation, or who asks for their data, needs one complete answer from every store: a full export, and a deletion that leaves nothing of theirs behind.

- **A user's data spans two databases.** SQL holds the `users` row, project memberships and per-user index overrides; Cosmos DB holds every session, turn and turn trace in `conversations` (key `[userId, sessionId]`), the memory document in `memories` and the counters and windows in `usage` (both keyed by `userId`). Sessions in projects the user has lost access to are hidden from the user, yet they are still the user's data, so export and deletion include them.
- **Conversation data is private.** It is open to debugging only through privileged, audited access, and reading another user's data is an operator action outside the app. No app operation, admin screens included, may expose it.
- **Some records must outlive the person.** `audit_events` is append-only and is the evidence of who did what, the deletion itself included; actor columns record who changed configuration; documents an admin uploaded belong to an index that serves many people.
- **Backups bring deleted data back.** Cosmos DB continuous backup (30 days in `prod`) and SQL point-in-time restore (14 days in `prod`) restore to a moment before a deletion.
- **A valid token can re-create data.** The API provisions a `users` row on any authenticated request (ADR 0021), and an access token issued before the user's app assignment is removed stays valid until it expires, 60 to 90 minutes after issue.

Vendor facts that bound the choice: a query on the leading level of a hierarchical key is a targeted cross-partition query that reaches only the physical partitions holding that prefix; point reads need the full key. Deleting all items by partition key value is a public preview feature that needs an account capability, takes only the full hierarchical key, never a prefix, and a point-in-time restore taken while it runs may contain the deleted items (checked 2026-10-18). A point-in-time restore creates a new account and restores the TTL settings.

## Decision

**Commands.** Three subcommands of the operator CLI (`uv run docchat-ops --env <dev|prod|load> user <command>`, `scripts/ops/src/docchat_ops/commands/users.py`, on the `docchat_core.cosmos` repositories and document models): `export`, `delete` and `verify`. They follow the CLI's rules (ADR 0139): the operator's own `az login` through `AzureCliCredential`, endpoints from `.azure/<env>/.env`, no credential ever as an argument; `--ticket <id>` (1 to 64 characters) and `--reason <text>` (10 to 500 characters) on every command; a dry-run summary and a confirmation for `delete` unless `--yes`, and in `prod` the operator also types the environment name; output files with mode 0600 in directories with mode 0700; a JSON report on stdout; exit codes 0 success, 1 unexpected error, 2 usage, validation or refused precondition (nothing written), 3 access denied, 4 not found, 5 incomplete (run the same command again), 6 conflict, 7 store unreachable.

**Access.** Every command runs under the just-in-time level `user_data` (ADR 0024): membership of `sg-docchat-{env}-user-data-operators`, at most 4 hours, granted by a second person. The group holds Cosmos DB Built-in Data Contributor at `/dbs/docchat` (or, when Cosmos DB doesn't honour a group as principal, the time-bound per-principal assignment that `docchat-ops jit grant` creates) and SQL role `docchat_user_data_operator`: `SELECT` on `app.users`, `app.project_members`, `app.projects`, `app.user_index_overrides`, `app.audit_events` and every table with an actor column; `DELETE` on `app.users`, `app.project_members`, `app.user_index_overrides`; `INSERT` on `app.audit_events`.

**Export** (`user export --user <oid> --out <dir>`):

1. SQL, in one read-only snapshot transaction: the `users` row; `project_members` rows joined to project names; `user_index_overrides` rows; `audit_events` rows whose `actor_object_id` or `subject_object_id` is the user; and, for each column marked as an actor column in the SQLAlchemy metadata (`info={"actor": True}`), the primary keys of the rows holding the user's ID.
2. Cosmos DB: every item of the prefix `[oid]` in `conversations` (`SELECT * FROM c WHERE c.userId = @userId`: every document type, every project, hidden sessions included), the user's `memory` document, and every item of the user's `usage` partition.
3. Package `subject/`, handed to the person: `profile.json` (display name, email, first and last seen, last known role); `memberships.json` (project ID and name, granted at); `sessions.json` (each session with project ID, project name or null, title and timestamps, and its turns with `seq`, status, question, answer, citations as `CitationView` (`number`, `title`, `pageNumber`, `headingPath`, `contentType`, `openAs`), follow-ups, feedback and timestamps); `memory.json` (paused flag; facts with text, category and timestamps); `usage.json` (day and month token counters). Package `internal/`, kept with the ticket: `conversations.jsonl`, `memory.json` and `usage.jsonl` (raw documents without system fields), `turn_traces.jsonl`, `audit_events.json`, `actor_references.json`. Plus `manifest.json`: user, environment, ticket, operator, created at, script version, item counts and the SHA-256 of every file. Citations are exported as references; the cited files are not part of the export.
4. One audit row `user_data.exported` (actor type `operator`, subject the user, target `user` / `oid`, metadata `ticket`, `counts` per store, `manifestSha256`).

**Delete** (`user delete --user <oid> --confirm <oid> --assignment-removed`):

1. Refuses with exit 2 unless `--confirm` equals `--user` and `--assignment-removed` is given: the operator attests that the tenant administrator removed the user's assignments to the web client and API enterprise applications, or deleted the account.
2. Prints the counts it will delete, then deletes every `conversations` item of the prefix `[oid]`, `turn_trace` and `turn` documents before `session` documents, 8 deletes at a time, a `404` counted as deleted; then the `memory` document; then every `usage` item.
3. In one SQL transaction: deletes the user's `user_index_overrides`, `project_members` and `users` rows and inserts `user_data.deleted` (metadata `ticket`, `counts` per store).
4. Counts every store again and exits 5 when anything remains.

It keeps every `audit_events` row; every actor column (`created_by`, `updated_by`, `archived_by`, `granted_by`, `set_by`, `deleted_by`, `uploaded_by`, `requested_by`) and `audit_events.actor_object_id` and `subject_object_id`, which hold only the object ID; and every organisational document and blob, those the user uploaded as an admin included.

**Verify** (`user verify --user <oid>`): counts the user's items in the three containers and rows in `users`, `project_members` and `user_index_overrides`; exit 0 when all are zero, 5 otherwise. It runs no sooner than 2 hours after `delete`, when every token issued before the assignment removal has expired.

**Deletion register.** The `user_data.deleted` rows and the `document.traces_redacted` rows of the legal-removal command (ADR 0149) are the register. Before the app is switched to a point-in-time restored Cosmos DB account or SQL database, the operator reads the register from the live database with the constant statement `SELECT action, subject_object_id, target_id, occurred_at FROM app.audit_events WHERE action IN ('user_data.deleted', 'document.traces_redacted') AND occurred_at >= :restore_point` (`:restore_point` bound as a parameter), runs `docchat-ops user delete` for each listed user and `docchat-ops user redact-document` for each listed document against the restored stores, and reopens the app only when every `verify` and every redaction exits 0. The restore keeps TTL enabled, never `restoreWithTtlDisabled`, so items past their retention expire again (ADR 0032).

**Runbooks** (`86-operations.md`). Export: ticket with the user's object ID; `user_data` grant; `az logout` then `az login`, so the new membership is in the token; export to an encrypted volume; deliver `subject/` through the organisation's data-request channel; keep `internal/` with the ticket; delete both packages 30 days after the ticket closes, or at once when a later redaction covers a document whose text a package holds; revoke. Deletion: the tenant administrator removes each DocChat app-role assignment (`GET https://graph.microsoft.com/v1.0/users/<oid>/appRoleAssignments`, then `DELETE .../appRoleAssignments/<assignment ID>`) or deletes the account; grant; `delete`, rerun on exit 5; revoke; no sooner than 2 hours later grant again, `verify`, rerun `delete` on exit 5, revoke and close the ticket.

## Alternatives considered

- **Self-service deletion in the app:** user-initiated deletion is out of scope for the first version, and an export needs traces and audit rows that no app operation may expose.
- **An admin screen for export and deletion:** admins would read other users' conversations through the app, which the product forbids; the operator path is just-in-time, two-person and logged per request.
- **Deleting or anonymising audit rows and actor columns:** the audit trail is append-only and is the evidence of the deletion itself; the columns hold only a random object ID, whose mapping to a name is the `users` row that the deletion removes.
- **Deleting documents the user uploaded as an admin:** documents belong to an index that serves many projects and users, not to the uploader.
- **Cosmos DB delete by partition key:** a preview feature, it can't take the `[userId]` prefix (one call per session would still be needed), and a restore taken while it runs may hold the deleted items. Per-item deletes are GA, idempotent and countable.
- **Sessions before turns and traces:** an interrupted run would leave turns and traces whose session is gone, a state no read path expects; children go first, and a rerun finds what remains by the same prefix query.
- **No register, waiting for backups to age out:** a restore inside the 30-day backup window would bring deleted conversations back without anyone noticing.

## Consequences

- One command answers each request completely, hidden sessions included, and `verify` proves the result; partial runs are safe, because every delete is idempotent, the SQL part is one transaction and exit 5 means run it again.
- The assignment must be removed before `delete`; otherwise the next request with a valid token would re-create the `users` row through first-sign-in provisioning and start new conversation items. The 2-hour wait before `verify` covers the 90-minute token lifetime.
- An export package holds conversation text, memory facts and traces, so it lives on an encrypted volume with mode 0600 files and is deleted 30 days after the ticket closes.
- After deletion the user's object ID remains in audit rows and actor columns, with no name or email anywhere in the product.
- Any point-in-time restore of Cosmos DB or SQL carries a mandatory step: re-apply the register before reopening the app (the restore runbook links to it).
- Deleting a heavy user issues one request per item, 8 at a time, so it takes minutes and consumes request units on the `conversations` container while it runs.
- Tests: integration against the emulators, a seeded user with sessions in reachable and unreachable projects, memory, usage, SQL rows, audit rows and actor references is exported completely with verifiable manifest hashes, `delete` removes everything except audit rows and actor columns, a second run changes nothing, `verify` exits 0 and another user's data is untouched; acceptance, a `dev` rehearsal of the deletion runbook on the test member account ends with `verify` exiting 0.

## References

- Hierarchical partition keys (prefix queries): https://learn.microsoft.com/en-us/azure/cosmos-db/hierarchical-partition-keys (checked 2026-10-14)
- Delete items by partition key value (preview): https://learn.microsoft.com/en-us/azure/cosmos-db/how-to-delete-by-partition-key (checked 2026-10-18)
- Continuous backup and point-in-time restore: https://learn.microsoft.com/en-us/azure/cosmos-db/continuous-backup-restore-introduction (checked 2026-10-14)
- Configurable token lifetimes (60 to 90 minutes by default): https://learn.microsoft.com/en-us/entra/identity-platform/configurable-token-lifetimes (checked 2026-10-14)
