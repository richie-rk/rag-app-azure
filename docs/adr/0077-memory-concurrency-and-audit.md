# ADR 0077: Memory concurrency and audit

- **Status:** Accepted
- **Date:** 2026-10-18
- **Decided by:** Architect
- **Specified in:** 13-conversations-and-memory.md, 11-api.md, 80-data-model.md, 82-security.md, 85-observability.md, 10-web-client.md, 84-testing.md, 86-operations.md
- **Related ADRs:** 0012, 0020, 0026, 0029, 0033, 0075, 0076, 0094, 0143

## Context

- Memory edits use the same optimistic concurrency as admin edits: the version travels in the response body and comes back in `If-Match`, and a stale write gets `412`. It travels in the body because the browser runs on another origin than the API: App Service platform CORS has only `allowedOrigins` and `supportCredentials`, with no expose-headers setting, and `ETag` isn't a CORS-safelisted response header, so a cross-origin script can't read it.
- Two writers change one `memory` document: the user, from any number of tabs and devices, and the background extraction (ADR 0075). Without a condition the later write silently discards the earlier one: an extraction prepared before a delete could bring the deleted fact back, or one tab could overwrite another tab's edit. The user must stay in control of what is remembered.
- In Cosmos DB a single-item replace is a transaction, and `_etag` with if-match gives `412` on mismatch. The `memory` document is one item of at most 200,000 bytes, so the client's version check and the write can be one atomic condition.
- Every memory change must leave an audit record of who changed what kind of thing and when, never the fact text, in the append-only `audit_events` table in SQL, so that any change to what the assistant remembers about a person can be attributed and scoped.
- The memory document lives in Cosmos DB and the audit table in SQL; no transaction spans both, and a committed Cosmos DB change can't be rolled back when the SQL insert then fails.

## Decision

**Version in the body.** Every `MemoryView` (from `getMemory`, `updateMemorySettings`, `updateMemoryFact`, `deleteMemoryFact` and `clearMemory`) carries `etag`: the memory document's Cosmos DB `_etag`, verbatim, double quotes included. No other Cosmos DB system field reaches a client. `getMemory` creates an empty document when none exists (`create_item`; a `409` means a concurrent request created it, so it is read), so a client holds an `etag` before its first write.

**`If-Match` on every write.** The four writes (`updateMemorySettings`, `updateMemoryFact`, `deleteMemoryFact`, `clearMemory`) require an `If-Match` header holding exactly one strong entity tag:

- missing, `*`, a weak tag (`W/"…"`), a list (any value containing a comma, repeated headers joined with commas) or a value not wrapped in double quotes → `428 precondition_required`;
- a tag that doesn't equal the stored `_etag` exactly → `412 precondition_failed`.

The web client sends the body's `etag` unchanged, because it already starts and ends with `"`.

**Order of checks** (lookups before preconditions, the API-wide order of ADR 0020), after the route class's pre-body checks (token, role, burst limit, per-user write limit, first-sign-in provisioning) and body validation:

1. Point-read the `memory` document in partition `[oid]`.
2. Lookup: `updateMemoryFact` (`PUT /v1/memory/facts/{factId}`) and `deleteMemoryFact` (`DELETE /v1/memory/facts/{factId}`) answer `404 memory_fact_not_found` when the fact, or the whole document, doesn't exist, whatever `If-Match` holds. `updateMemorySettings` (`PATCH /v1/memory`) and `clearMemory` (`DELETE /v1/memory/facts`) have no lookup.
3. Preconditions: `428`, then `412`. With no `memory` document, a well-formed tag can't match, so `updateMemorySettings` and `clearMemory` answer `412`.
4. The operation's own checks (fact validation, caps and screening, ADR 0075) and no-op detection: an unchanged `paused` value, an unchanged fact text and category, or clearing an empty memory answers `200` with the current view, with no write and no audit row.
5. `replace_item` with `etag` = the same `_etag` and `match_condition` = `MatchConditions.IfNotModified`; a `412` here (a change between the read and the write) answers `412 precondition_failed`. The check and the write are one atomic condition.
6. After the commit, the audit row below; then `200` with the new `MemoryView` and its new `etag`.

Extraction writes are conditioned on the document's `_etag` too (re-read, re-validate, at most 3 attempts; ADR 0075), so every extraction changes the version and an edit prepared before it gets `412`. On `412` the memory screen refetches `getMemory`, shows the conflict text, keeps an open editor's text when its fact still exists and closes the editor when it doesn't (ADR 0094).

**Audit after the commit.** Every memory change writes exactly one `app.audit_events` row after the Cosmos DB write commits, through `docchat_core.audit.write_audit_event_after_commit(event)`, the one path for changes committed outside SQL (ADR 0029). The memory code builds the `AuditEvent` (action, target and the per-action metadata model) and calls the helper once; it has no retry loop, event or counter of its own.

| `action` | `actor_type`, actor | `subject_object_id` | Target type, ID | `metadata_json` keys |
|---|---|---|---|---|
| `memory.paused` | `user`, caller | caller | `memory`, caller's `oid` | none |
| `memory.resumed` | `user`, caller | caller | `memory`, `oid` | none |
| `memory.fact_updated` | `user`, caller | caller | `memory_fact`, fact ID | `category`, `previousCategory`, `previousSourceKind` |
| `memory.fact_deleted` | `user`, caller | caller | `memory_fact`, fact ID | `category`, `sourceKind` |
| `memory.cleared` | `user`, caller | caller | `memory`, `oid` | `factsRemoved` |
| `memory.facts_extracted` | `system`, the API identity's principal ID (`DOCCHAT_CORE_WORKLOAD_PRINCIPAL_ID`) | the user | `memory`, `oid` | `addedFactIds`, `replacedFactIds`, `categories`, `sessionId`, `turnId` |

Metadata holds IDs, categories and counts only, never fact text. The helper runs the insert as its own unit of work and retries 3 times, after 0.2, 0.8 and 2 seconds (SQL error 40613 included). When every attempt fails it emits the custom event `audit_write_failed` (`action`, `actorType`, `targetType`, `targetId`, `sessionId`, `turnId`) with one error log record, increments `docchat.audit.write_failures` (dimension `action`) and returns without raising; the operation still answers `200`, because the change is committed. The alert `audit-write-failures` fires when that counter is above 0 over 15 minutes, evaluated every 5 minutes, severity 2, and leads to the incident-response runbook.

**Limits.** The four writes pass the per-user burst limit (`429 too_many_requests`) and write limit (`429 write_rate_limited`, or `503 dependency_unavailable` when the limit store can't be reached, failing closed; ADR 0143); the two body routes, `updateMemorySettings` and `updateMemoryFact`, take at most 65,536 bytes of `application/json`.

## Alternatives considered

- **`ETag` response header.** Rejected: the cross-origin browser can't read it, and platform CORS can't expose it.
- **A version counter inside the document.** Rejected: Cosmos DB maintains `_etag` atomically with every write; a second counter needs its own conditional write and can disagree with it.
- **Last write wins.** Rejected: a background extraction prepared before a user's delete would undo it, which breaks the promise that the user controls their memory.
- **Per-fact patches with a filter predicate.** Rejected: the caps and the duplicate check compare each fact with every other fact, so the whole set must be checked against one version; one conditional replace keeps that atomic.
- **Audit row first, then the Cosmos DB write.** Rejected: a `412` or an outage would leave a row for a change that never happened, in a table nobody may delete from.
- **An outbox relayed to SQL by a background process, or the Cosmos DB change feed.** Rejected: a relay or change-feed processor needs leases and a consumer that runs all the time, and the change feed doesn't say who acted, for a gap the alert already exposes.
- **Failing the request when the audit insert fails.** Rejected: the change is committed, so the error would be false and a retry would meet `412`.
- **Memory audit rows in Cosmos DB.** Rejected: one append-only `audit_events` table holds every audited action, so one query answers who changed what.

## Consequences

- Two tabs editing memory: the second write gets `412` and refetches; a user editing while an extraction lands sees `412` once.
- A fact deleted in another tab answers `404 memory_fact_not_found` to a stale edit or delete, never `412`, so the client can tell "gone" from "changed".
- A failed audit insert leaves a committed change without its row; the `audit_write_failed` event and its error log record are the record of the gap, and the alert surfaces it within 15 minutes.
- No-op requests leave no audit rows, so the trail lists only real changes.
- Audit rows are kept indefinitely and survive user-data deletion (ADR 0033); they hold object IDs and fact IDs only.
- Every write replaces the whole document (at most 200 facts at the `memory.max_facts` ceiling, 200,000 bytes).
- Monitoring: `docchat.memory.user_changes` by `action`; spans `docchat.memory.update_settings`, `docchat.memory.update_fact`, `docchat.memory.delete_fact` and `docchat.memory.clear` with outcomes including `precondition_failed` and `precondition_required`.
- Tests: `If-Match` parsing (missing, `*`, weak or a list → `428`; one strong tag passes); two `updateMemoryFact` calls with one ETag give one `200` and one `412`, and an extraction write between `getMemory` and an edit makes the edit `412`; with SQL unavailable during an edit the API answers `200`, the change is committed and exactly one `audit_write_failed` event and one counter increment appear; lookup before precondition (a fact deleted in another tab answers `404 memory_fact_not_found` with a stale or missing `If-Match`; with no document, `deleteMemoryFact` answers `404` and `clearMemory` `412`); an extraction writes `memory.facts_extracted` with no text; a stale edit in the browser shows the refresh prompt; the helper retries 3 times, then emits the event without raising; cross-user isolation on every memory operation.

## References

- Cosmos DB transactions and optimistic concurrency (`_etag`, if-match, `412`): https://learn.microsoft.com/en-us/azure/cosmos-db/database-transactions-optimistic-concurrency (checked 2026-10-14)
- `azure.cosmos.aio.ContainerProxy` (`etag` and `match_condition`): https://learn.microsoft.com/en-us/python/api/azure-cosmos/azure.cosmos.aio.containerproxy (checked 2026-10-15)
- App Service platform CORS (`allowedOrigins`, `supportCredentials`): https://learn.microsoft.com/en-us/azure/app-service/app-service-web-tutorial-rest-api (checked 2026-10-14)
- CORS-safelisted response headers: https://developer.mozilla.org/en-US/docs/Glossary/CORS-safelisted_response_header (checked 2026-10-14)
- Azure SQL transient errors (40613): https://learn.microsoft.com/en-us/azure/azure-sql/database/troubleshoot-common-errors-issues?view=azuresql (checked 2026-10-14)
