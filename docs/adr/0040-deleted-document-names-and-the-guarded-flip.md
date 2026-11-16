# ADR 0040: Deleted document names and the guarded Flip

- **Status:** Accepted
- **Date:** 2026-10-18
- **Decided by:** Architect
- **Specified in:** 11-api.md, 14-ingestion.md, 80-data-model.md, 84-testing.md, 86-operations.md, CONTEXT.md
- **Related ADRs:** 0031, 0032, 0034, 0037, 0041, 0042, 0045, 0061, 0069, 0094, 0149

## Context

A document is one row per (logical index, normalised name), and each uploaded version is identified by the SHA-256 of its bytes (ADR 0034). Readers see only the chunk sets the Flip makes served, so a changed file replaces its predecessor completely, and an admin's delete must hide a document at once and keep it hidden. Three needs meet here:

- **A deleted name must be usable again.** `documents` is unique on (index, normalised name), so a second row with a deleted document's name can't exist; without a rule, that name could never be uploaded again in its index.
- **A deleted document's bytes must not block a re-upload.** Filtered unique indexes on (document, SHA-256) and (index, SHA-256) cover versions in `pending`, `ingesting` and `ingested`; a deleted document's `ingested` version left inside them would make the same file answer `content_unchanged` or `duplicate_content` against a document nobody can see.
- **A Flip racing a delete must never bring the document back.** Azure SQL Database runs READ_COMMITTED_SNAPSHOT by default: a plain read sees a snapshot of committed data and takes no lock, so a Flip that read `is_deleted = 0` and then wrote would make a document current again if a delete committed between its two statements. A data-modification statement, by contrast, qualifies rows against the latest committed data and locks the rows it changes: without optimized locking through a blocking scan with update locks, and with optimized locking by qualifying on the latest committed version, then taking exclusive locks, with update conflicts detected and retried by the engine. Either way, a guarded UPDATE whose row count is checked serialises with a concurrent delete of the same row.

A delete also arrives while the document's items may be running, and a deleted document must stay deleted whatever those items do next.

## Decision

1. **Identity.** `app.documents` holds one row per (logical index, normalised name), enforced by `UQ_documents_index_name` unique (`search_index_id`, `normalized_name`). `documents.id` is a random UUID (version 4) created at the first reservation of a name and never changed, including when a deleted document comes back. `normalize_document_name`: Unicode NFC, trim, collapse each whitespace run to one space, `str.casefold()`; the name after NFC and trimming is kept as `source_name` for display. Version numbers come from `documents.next_version_number`, taken and incremented under the row's update lock, so they are never reused and every version has its own blob path `documents/{indexId}/{documentId}/{versionNumber}/source`.
2. **Deleting a document.** `adminDeleteDocument` (`DELETE /v1/admin/indexes/{indexId}/documents/{documentId}`, `If-Match` with the document's `rowVersion`, `409 index_busy` while the index is `deleting`) runs one transaction:
   - `UPDATE app.documents SET is_deleted = 1, deleted_at = SYSUTCDATETIME(), deleted_by = @actor, current_version_id = NULL WHERE id = @document AND is_deleted = 0 AND row_version = @rowVersion`; zero rows answers `412 precondition_failed` when the document is live with another row version and `404 document_not_found` when it is already deleted;
   - every version in `pending`, `ingesting` or `ingested` becomes `superseded`, and every version in `uploading` becomes `failed` with `document_deleted`;
   - every `served_chunk_sets` row of the document, in every physical index, is deleted, which hides it from readers at once, and its `building`, `ready` and `served` chunk sets become `retired`;
   - its `queued` items become `cancelled` with `document_deleted`;
   - a `document_delete` run (priority 0) gets one `cleanup_document` item, and audit action `document.deleted` records `indexId`, `runId`, `supersededVersionCount` and `cancelledItemCount`.

   After commit the API sends one queue message (ADR 0037) and answers `202 {runId}`. The clean-up item deletes the retired chunk sets' chunks from every `active` or `building` physical index and the superseded versions' renditions and source blobs, which stay soft-deleted for 14 days (ADR 0031). Running items finish, and their Flip fails its guard (rule 4). Citations to the document answer `410 document_changed`. The `documents` and `document_versions` rows are kept: they back the `410` answers and the return of the name (rule 3).
3. **A deleted name comes back as the same document.** Preflight answers `new` for a deleted document's name. `adminReserveUpload` reads the row with `SELECT ... FROM app.documents WITH (UPDLOCK, ROWLOCK) WHERE search_index_id = @index AND normalized_name = @norm` and, for a deleted row, runs `UPDATE app.documents SET is_deleted = 0, deleted_at = NULL, deleted_by = NULL, source_name = @name, updated_at = SYSUTCDATETIME(), updated_by = @actor WHERE id = @doc` in the same transaction that adds the new `uploading` version. The document ID stays, and `current_version_id` stays null until the new version's Flip. Because the delete superseded every active version, neither filtered SHA-256 index blocks the same bytes, under this name or another. A unique violation of `UQ_documents_index_name` (two first reservations racing) rolls back and retries the whole transaction once.
4. **The guarded Flip.** `flip_chunk_set` runs one transaction under READ COMMITTED with RCSI, in which every guard is a locking write or an `UPDLOCK` read: first `SELECT active_physical_index_id FROM app.search_indexes WITH (UPDLOCK, ROWLOCK) WHERE id = @index` (it serialises Flips, repoints and re-ingest starts of the logical index, ADR 0041), then the fenced lease update of the item (`... WHERE id = @item AND claim_count = @claim AND state = 'running'`; zero rows ends the attempt with `lease_lost`), then the version's row read `WITH (UPDLOCK)`. The decision on `app.documents` is one guarded UPDATE whose row count is checked, and no plain read of `documents` precedes it:
   - (a) into the logical index's active physical index, for a version that isn't current:

     ```sql
     UPDATE app.documents SET current_version_id = @version, updated_at = SYSUTCDATETIME()
     OUTPUT deleted.current_version_id INTO @prev
      WHERE id = @document AND is_deleted = 0
        AND (current_version_id IS NULL OR current_version_id <> @version)
        AND NOT EXISTS (SELECT 1 FROM app.document_versions n
                         WHERE n.document_id = @document AND n.id <> @version
                           AND n.state IN ('pending','ingesting','ingested')
                           AND (n.committed_at > @committed_at
                                OR (n.committed_at = @committed_at AND n.version_number > @vnum)));
     ```

   - (b) any other Flip (the current version re-ingested or rebuilt, in the active or the building index): `UPDATE app.documents SET updated_at = SYSUTCDATETIME() WHERE id = @document AND is_deleted = 0 AND current_version_id = @version`.
   - Zero rows: the chunk set becomes `retired`, a version still `ingesting` becomes `superseded`, and the item is skipped with `document_deleted` when the document is deleted and `superseded_by_newer` otherwise; the transaction commits with nothing served.
   - One row: in case (a) the version it replaces becomes `superseded`; this version becomes `ingested` (`WHERE state IN ('ingesting','ingested')`; zero rows rolls the whole transaction back as `internal_error`); the chunk set served for (document, this physical index) becomes `retired`, the `served_chunk_sets` row points at the new chunk set, and that chunk set becomes `served`. From the commit on, readers see only the new chunk set. ADR 0041 adds the building-index item that a case (a) Flip queues during a rebuild.
5. **Why every order is safe.** The delete and the Flip both write the same `documents` row, so they serialise on it. A delete committed first leaves the Flip's guard with zero rows. A delete still open makes the Flip wait, then the guard finds `is_deleted = 1`. A Flip committed first changes the row's `row_version`, so a delete sent with the earlier `rowVersion` answers `412`, and the admin's repeated delete removes what the Flip served. A newer committed version beats an older build the same way, through the `NOT EXISTS` clause.

## Alternatives considered

- **Refuse a deleted name until an operator purges its rows.** The name would be unusable in its index, and purging rows would break the `410` answers of earlier citations and orphan the audit and run rows that reference them.
- **Delete the document and version rows.** Earlier citations would answer `404` where `410 document_changed` tells the reader the document changed, and runs, items and audit rows would lose the rows they name.
- **A new document row for each re-upload of a deleted name**, with the unique index filtered to live rows. One name in one index would then have several identities, one document's history would split over several IDs, and version numbers would restart, needing another rule to keep blob paths unique.
- **Flag the document but leave its versions in their states.** The filtered SHA-256 indexes would keep blocking the same bytes, so re-uploading a deleted file would fail against an invisible document.
- **A check-then-write Flip.** Under RCSI its read takes no lock, so a delete committing between the read and the write would be undone.
- **`SERIALIZABLE` or `REPEATABLE READ` for the Flip.** Range and read locks held for the whole transaction would block uploads and deletes and invite deadlocks; one guarded UPDATE gives the same guarantee.
- **Cancel running items at delete.** The API can't reach into running sub-orchestrations, and Durable termination doesn't propagate to them; the guard makes their result harmless.

## Consequences

- A deleted document stays deleted even when one of its items is running, and readers stop seeing it in the delete's own transaction. Only the Flip's guarded UPDATE sets `documents.current_version_id`, and only the delete clears it.
- Re-uploading a deleted name keeps the document ID, continues its version numbers and starts with no current version; turns that cite a deleted version keep answering `410 document_changed`.
- An admin's delete that loses to a concurrent Flip or reservation answers `412 precondition_failed`, and it succeeds once sent again with the reloaded document's `rowVersion` (ADR 0094).
- A version whose item was running when its document was deleted may reach its Flip after the name came back and before the new version commits: the document guard then passes (not deleted, no newer committed version), the version-state check finds the version `superseded`, and the whole Flip rolls back as `internal_error`. The item retries (up to `ingestion.retry_max_attempts`, default 7) and is skipped `superseded_by_newer` once the new version commits, or fails `internal_error` when it never does; the deleted version is never served again.
- `documents` and `document_versions` rows are never purged by retention; their blobs follow the clean-up rules (ADR 0031, ADR 0032).
- The guarantee holds only while every write of `documents.current_version_id` goes through the Flip or the delete flow: `docchat_config_operator` holds only `SELECT` on `app.documents`, and no migration may set the column.
- Tests: a deleted name comes back with the same ID, and two concurrent first reservations create one document; a Flip against a concurrent delete on two connections under RCSI ends, in either order, with the document deleted and no served row; an older version's Flip after a newer one committed ends `superseded` with its chunk set retired; the delete flow's state changes, cancellations and clean-up run; a delete during ingestion leaves the document deleted, nothing served, and its chunks, renditions and source removed; with several pending versions only the newest committed is ingested; delete, then re-upload of the same name gives the same document ID, the new version ingested and the old chunks cleaned.
- Runbook (86-operations.md, "Delete a document"): the admin deletes on the Documents screen with the document's `rowVersion` and follows the returned run; a `cleanup_failed` item is retried by sweep duty 6; a legal removal continues with ADR 0149's removal of the document's text from stored traces.

## References

- READ_COMMITTED_SNAPSHOT is on by default in Azure SQL Database: https://learn.microsoft.com/en-us/sql/t-sql/statements/set-transaction-isolation-level-transact-sql (checked 2026-10-14)
- Data modification under row versioning, with and without optimized locking; update locks: https://learn.microsoft.com/en-us/sql/relational-databases/sql-server-transaction-locking-and-row-versioning-guide (checked 2026-10-18)
- `rowversion` and the optimistic-concurrency pattern: https://learn.microsoft.com/en-us/sql/t-sql/data-types/rowversion-transact-sql (checked 2026-10-14)
- Filtered unique indexes: https://learn.microsoft.com/en-us/sql/t-sql/statements/create-index-transact-sql?view=azuresqldb-current and https://learn.microsoft.com/en-us/sql/relational-databases/indexes/create-filtered-indexes (checked 2026-10-14)
