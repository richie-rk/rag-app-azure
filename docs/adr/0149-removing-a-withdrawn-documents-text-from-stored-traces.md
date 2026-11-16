# ADR 0149: Removing a withdrawn document's text from stored traces

- **Status:** Accepted
- **Date:** 2026-10-21
- **Decided by:** Architect
- **Specified in:** 13-conversations-and-memory.md, 80-data-model.md, 82-security.md, 84-testing.md, 86-operations.md
- **Related ADRs:** 0024, 0029, 0031, 0033, 0040, 0063, 0071, 0139, 0142

## Context

- Every finalised turn writes one write-once `turn_trace` (ADR 0071). Its `modelInput.messages` hold the answer call exactly as sent, including each kept source's chunk text in a `SOURCE` block between the lines `[[BEGIN SOURCE S<n> <marker>]]` and `[[END SOURCE S<n> <marker>]]`, where `<marker>` is drawn per call and stored as `modelInput.marker` (ADR 0063). Its `retrieval.kept` lists at most 16 kept sources, each with `sourceId` (null for a source dropped by screening), `chunkKey` and `documentId`.
- The product owner keeps conversations indefinitely. Traces are read only through just-in-time access (ADR 0024) and are copied into the operator export's `internal/` package (ADR 0033).
- Deleting a document (ADR 0040) hides it from readers at once and removes its chunks, renditions and blobs, which stay soft-deleted for 14 days (ADR 0031). Text already copied into traces stays. When a document must be withdrawn for a legal reason, its text has to leave every place the platform keeps it, traces included.
- Cosmos DB facts (checked 2026-10-21): array elements are addressed in index paths with `/[]`, so `/retrieval/kept/[]/documentId/?` indexes every kept entry's document ID; a container that excludes the root path `/*` indexes only its included paths; a replace conditioned on an ETag fails with `412` when the item changed (checked 2026-10-14); a point-in-time restore brings back the account as it was at the restore time, so a restore undoes any later change.

## Decision

1. **Findable traces.** The `conversations` indexing policy includes `/retrieval/kept/[]/documentId/?` among its included paths (ADR 0071), declared in the container definition that creates the container, so the query in item 3 is index-backed.
2. **The command.** `docchat-ops user redact-document --document-id <uuid> --ticket <id> --reason <text>`, at the `user_data` just-in-time level (`sg-docchat-<env>-user-data-operators`: Cosmos DB Built-in Data Contributor at `/dbs/docchat` and the SQL role `docchat_user_data_operator`). It prints the number of matching traces and asks for confirmation (in `prod`, the operator types the environment name). It doesn't need the document's SQL row, so it also works after the document's clean-up.
3. **Find.** One cross-partition query with bound parameters, read in pages of 100: `SELECT c.id, c.userId, c.sessionId FROM c WHERE c.type = @type AND EXISTS(SELECT VALUE k FROM k IN c.retrieval.kept WHERE k.documentId = @documentId)`, with `@type` = `turn_trace`.
4. **Change.** For each hit, point-read the trace by its full key and, in every message of `modelInput.messages`, replace the body of each `SOURCE` block whose source ID is the non-null `sourceId` of a `retrieval.kept` entry with that `documentId` (the lines between its `BEGIN` and `END` lines, which use `modelInput.marker`) with the single line `[removed]`, and set `redactedAt` to now (UTC, milliseconds). The replace is conditioned on the item's ETag and changes no other field; a `412` re-reads and reapplies, at most 3 attempts, and a trace still conflicting ends the run with exit 6. A trace with no block left to change (already redacted, `modelInput` null or `{"omitted": "size_limit"}`, or the source never sent) isn't written.
5. **The one permitted change.** This replace is the only change ever made to a written trace (ADR 0071). Answers, summaries, follow-ups and citation titles are the model's wording shown to the user, not the document's text, and stay unchanged, even where they quote the document: each turn's `answer`, each session's `summary.text`, and inside each trace's `modelInput` the earlier answers sent as `assistant` messages and the `CONVERSATION_SUMMARY` block. No procedure deletes one session or edits one stored answer; the user-data deletion (ADR 0033) removes them only together with all of that user's conversation data, memory, memberships and app-role assignments. Security design section 1.6 accepts this residual risk.
6. **The record.** After the last page, one `audit_events` row `document.traces_redacted`: actor `operator`, subject null, target type `document`, target ID the document ID, metadata `{documentId, traceCount, ticket}`, where `traceCount` is the number of traces this run changed and `ticket` the command's `--ticket` value. The command is idempotent: a second run changes no trace, records `traceCount` 0 and exits 0; a run stopped part-way exits 5 and a rerun finishes it; Cosmos DB or SQL unreachable exits 7.
7. **The deletion register.** `document.traces_redacted` rows join the `user_data.deleted` rows of ADR 0033 as the deletion register. Before the app is switched to a point-in-time restored Cosmos DB account or SQL database, the operator lists the register from the live database with the constant statement `SELECT action, subject_object_id, target_id, occurred_at FROM app.audit_events WHERE action IN ('user_data.deleted', 'document.traces_redacted') AND occurred_at >= :restore_point` (`:restore_point` bound as a parameter), runs `docchat-ops user delete` for each listed user and `docchat-ops user redact-document` for each listed document against the restored stores, and reopens the app only when every `verify` and every redaction exits 0.
8. **The legal-removal runbook.** A ticket names the document ID and the legal basis; an admin deletes the document in the Documents screen (`adminDeleteDocument`); a group owner grants `user_data` for at most 4 hours (PIM, or `docchat-ops jit grant`, which refuses a grant to its own caller); the operator signs in again so the token carries the membership; runs the command until it exits 0; verifies with one more run that reports `traceCount` 0; revokes; and deletes every `internal/` export package kept with an open ticket that holds the document's text. The replaced text can't be brought back.

## Alternatives considered

- **Leave traces as written.** Rejected: a withdrawn document's text would outlive its deletion in every trace that used it, indefinitely, readable through just-in-time access and copied into exports.
- **Delete the traces or turns that cite the document.** Rejected: it destroys the record of every other source in those turns and the user's own conversation, which the user can still read.
- **Store chunk keys instead of chunk text in traces.** Rejected: each turn stores the exact text sent to the model, which is also the raw material for a later retrieval-quality evaluation.
- **Find traces without the indexed path.** Rejected: the container indexes only listed paths, so the query would scan every document in every partition; the indexed path keeps the legal removal practical as conversations grow.
- **Redact automatically on every document delete.** Rejected: updates and ordinary deletions aren't legal removals, and a trace is evidence of what the model saw; it changes only through a deliberate, ticketed and audited operator action.
- **Redact answers, summaries and follow-ups too.** Rejected: they are the model's wording shown to the user, not the document's text.

## Consequences

- A legal removal reaches every copy of a document's text: the search index and blobs through the document's clean-up, traces through this command, exports through the runbook, and restores through the deletion register.
- A redacted trace can't reproduce its answer call exactly; `redactedAt` marks it, and its `retrieval` record still says which document was sent.
- Indexing the path adds a little write cost per trace (at most 16 entries); the query runs only as an operator command, never on a request path.
- Restores carry a mandatory step: re-apply the register before reopening the app.
- Obligations (84-testing.md): an integration test against the emulators in which traces of two users cite document D, whose chunk text holds a canary, and one trace cites only document E; after the command no trace contains D's canary, every field of each changed trace other than D's `SOURCE` block bodies, `redactedAt` and the Cosmos DB system fields is byte-identical, `redactedAt` is set, E's trace is untouched and one `document.traces_redacted` row has `traceCount` 2; a second run changes nothing, records `traceCount` 0 and exits 0; a run stopped after the first trace exits 5 and a rerun completes. A CLI test that the command is registered at the `user_data` level and exits 6 when ETag conflicts remain after 3 attempts. The legal-removal and restore runbooks (86-operations.md); the audit action and its metadata model; the testable statement in 82-security.md.

## References

- Azure Cosmos DB indexing policies (array paths with `/[]`, excluding the root path): https://learn.microsoft.com/en-us/azure/cosmos-db/index-policy (checked 2026-10-21)
- Azure Cosmos DB optimistic concurrency: https://learn.microsoft.com/en-us/azure/cosmos-db/database-transactions-optimistic-concurrency (checked 2026-10-14)
- Azure Cosmos DB continuous backup and point-in-time restore: https://learn.microsoft.com/en-us/azure/cosmos-db/continuous-backup-restore-introduction (checked 2026-10-14)
