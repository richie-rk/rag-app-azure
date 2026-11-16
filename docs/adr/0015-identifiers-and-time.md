# ADR 0015: Identifiers and time

- **Status:** Accepted
- **Date:** 2026-10-21
- **Decided by:** Architect
- **Specified in:** 80-data-model.md, 11-api.md, 13-conversations-and-memory.md, 14-ingestion.md, 84-testing.md, CONTEXT.md
- **Related ADRs:** 0012, 0017, 0021, 0030, 0044, 0056, 0071

## Context

Every store, path, key and message in this product names entities by ID: SQL rows, Cosmos DB documents and partition keys, blob paths, search chunk keys, queue messages, Durable instance IDs, API paths and telemetry attributes. One representation for every ID, chosen once, lets each component hand IDs to the next without conversion, and lets path parameters be validated before any lookup. Some IDs must be derivable, so that a retried request finds the record its first attempt created (ADR 0056). Times cross the same boundaries, and lease, deadline, retention and quota arithmetic all compare times written by different processes.

Two needs shape the user and session keys:

- A user key must be immutable and unique. An e-mail address, or its local part, is shared by people on different domains and re-issued to new employees, so a store keyed by it would show one person another's projects and conversations, and hand a new holder the previous holder's sessions and memberships.
- A session key must never be chosen by the browser. A number allocated client-side collides between two tabs of one user and between users, and as a partition key it would put many users' sessions into one partition.

Facts that bound the design:

- In a v2 access token, `oid` is an immutable GUID and `tid` the tenant GUID; `preferred_username` and `name` are mutable and not for authorisation.
- A version-5 UUID is RFC 9562's SHA-1 name-based UUID: the same namespace and name always give the same value. A version-4 UUID carries 122 random bits.
- SQL Server's `UNIQUEIDENTIFIER` is a 16-byte GUID whose ordering isn't the comparison of its bit patterns or its text; `DATETIME2(3)` stores milliseconds in 7 bytes and keeps no time-zone offset; `SYSUTCDATETIME()` returns the server's UTC time.
- Cosmos DB `id` values and partition key values are strings compared exactly, so two spellings of one UUID name two items.
- Durable orchestrators replay, so they must read the time from `context.current_utc_datetime` and must not call the `uuid` module (the context's `new_guid()` returns version-5 UUIDs that are safe for replay).

## Decision

1. **Entity IDs are UUIDs.** Users, projects, logical and physical indexes, documents, document versions, chunk sets, ingestion runs and run items, sessions, turns, turn traces and memory facts are identified by UUIDs. Text form everywhere outside SQL (JSON, Cosmos DB, blob paths, search keys, queue messages, telemetry): canonical lower-case hyphenated, 36 characters. SQL: `UNIQUEIDENTIFIER`. Python: `uuid.UUID`, rendered with `str()`.
2. **Users are their Entra object ID.** A user is the validated token's `oid`; `app.users` is keyed by `object_id`; the user level of every Cosmos DB partition key is `str(principal.oid)` and comes only from the token. E-mail and display name are display data on the `users` row: never a key, never an input to a decision, never in a log.
3. **Random IDs are version 4.** A new entity's ID is generated with `uuid.uuid4()` by the component that creates the row or document, before the write, so the ID is known without a round trip; a set-based SQL `INSERT … SELECT` uses `NEWID()`, also a random version-4 value. Facts in a memory document are version 4.
4. **Deterministic IDs are version 5 over constant namespaces.** Names are UTF-8.

   | ID | Namespace | Name |
   |---|---|---|
   | New session ID | `60191fe1-b79c-4c47-995d-e0a0e20cb164` | `"{oid}:{clientTurnId}"` |
   | Turn ID | `fc6c167c-bdc0-407d-9fcb-0bb4c6a2c701` | `"{sessionId}:{clientTurnId}"` |
   | Turn trace ID | `fc6c167c-bdc0-407d-9fcb-0bb4c6a2c701` | `"{turnId}:trace"` |

   The memory document's `id` is the user's `oid`. The namespaces are constants in `docchat_core.cosmos.conversations.ids` and never change: a changed namespace would give a retried request a new ID and duplicate its session or turn.
5. **Inbound IDs.** IDs in paths, query strings and bodies must match `^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$` and are lower-cased after validation; braces, URN and 32-hex forms are `422 validation_failed` with reason `invalid_uuid`, so a malformed ID never reaches a lookup and a `404` always means "well-formed, and absent or unreachable".
6. **Keys that aren't entity UUIDs, each for a stated reason:**
   - `app.audit_events.id` is `BIGINT IDENTITY(1,1)`: an append-only log row that no other row references and no API returns; an ascending clustered key keeps every insert at the end of the index.
   - Singleton and keyed configuration rows: `app.ingestion_dispatcher_state` has the one row `id = 1`; `app.ops_config` is keyed by its dotted key; `app.ingestion_methods` by method key and version.
   - `usage` documents are counters and windows in the user's partition with computed IDs, so every increment is a point operation: `day:{YYYY-MM-DD}`, `month:{YYYY-MM}`, `rate:{epochMinute}`, `wrate:{epochMinute}`, `dlrate:{epochMinute}`, `lease:{slot}` and `dllease:{slot}`, where `epochMinute` is the number of whole minutes since 1970-01-01T00:00:00Z.
   - A document version also has `version_number`, taken from the document's `next_version_number` (starting at 1, never reused) and written in blob paths without padding; the version's identity stays its UUID.
   - Search chunk keys are `{documentId}_{chunkSetId}_{ordinal}` (ADR 0044); Durable instance IDs are `docchat-ingestion-dispatcher` and `docchat-item-{itemId}-{claimNumber}`; the correlation ID is the 32-character lower-case hex W3C trace ID.
7. **Time is UTC everywhere.**
   - SQL: `DATETIME2(3)`, defaults `SYSUTCDATETIME()`, and expiry comparisons inside SQL against `SYSUTCDATETIME()`.
   - API JSON and Cosmos DB documents: ISO 8601 strings with milliseconds and `Z`, as in `2026-10-14T09:30:00.000Z`.
   - Python: only timezone-aware `datetime` values in UTC; Ruff's `DTZ` rules fail any naive construction. Code reads the wall clock and the monotonic clock through a clock port (`now()`, `monotonic()`), which tests replace with `docchat_testing.clock.FakeClock` (`advance(seconds)`); elapsed-time measurements (rate buckets, timeouts) use the monotonic clock, stored instants the UTC wall clock.
   - Orchestrators read time only from `context.current_utc_datetime`, which replays deterministically, and generate no IDs: the activity that writes a row generates its ID, and the orchestrator receives it in the activity's result.
   - Calendar periods are UTC: a usage day is a UTC date and a usage month a UTC month.
   - The web client shows times in the browser's locale and time zone through `Intl.DateTimeFormat` and sends none.

## Alternatives considered

- **E-mail address, UPN or a part of one as the user key.** Rejected: neither is immutable nor unique across domains, and a re-issued address would hand one person's data to another.
- **Integer identity columns as entity keys.** Rejected: IDs would exist only after the insert, they would be guessable and sequential in URLs, and Cosmos DB documents and blob paths created before or beside a SQL row couldn't carry them.
- **Time-ordered UUIDs (version 7) for new rows.** Rejected: no order depends on the ID's value, because every list pages by keyset over its own sort columns with a unique column last (ADR 0012), and version 7 would expose each entity's creation time in every URL.
- **Client-generated session IDs.** Rejected: the browser chooses only the `clientTurnId`; the server derives session and turn IDs from it and the caller's `oid`, so a duplicate submit converges and no ID chosen by one user can land in another's partition.
- **A lookup table from `clientTurnId` to the created IDs.** Rejected: an extra write per turn, and a race between two submits that version 5 avoids by construction.
- **Local times or offsets in stored values.** Rejected: comparisons across processes and daylight-saving changes would need conversions; the client formats for display only.
- **`DATETIME2(7)` in SQL.** Rejected: millisecond precision matches the API and Cosmos DB formats, so a value read from one store and compared with another never differs in sub-millisecond digits.

## Consequences

- Any ID can travel from SQL to Cosmos DB, a blob path, a search key or a log record without conversion, and a duplicate request derives the same session and turn IDs.
- Cross-user access through an ID is impossible by construction: the user level of every partition key comes from the token, and a guessed session ID resolves inside the guesser's own partition.
- Obligations: the canonical text form is enforced at every boundary; the version-5 namespaces are part of the stored data's contract and appear in 13-conversations-and-memory.md; a new non-UUID key needs a reason recorded beside this list.
- Tests: upper-case UUIDs are accepted and lower-cased, while braces, URN and 32-hex forms give `422 invalid_uuid` in path, query and body; the version-5 derivations match known expected values; naive datetimes fail Ruff; every serialised time matches the millisecond `Z` pattern; lease and deadline tests run on `FakeClock`.
- Risk: SQL Server doesn't order `UNIQUEIDENTIFIER` values by their bit patterns or their text, while Cosmos DB orders IDs as strings; a keyset cursor's ID tie-breaker is therefore always compared by the store that issued it, never in Python.

## References

- Microsoft identity platform access token claims (`oid`, `tid`, `preferred_username`): https://learn.microsoft.com/en-us/entra/identity-platform/access-token-claims-reference (checked 2026-10-14)
- RFC 9562, Universally Unique IDentifiers (versions 4, 5 and 7): https://www.rfc-editor.org/rfc/rfc9562 (checked 2026-10-21)
- Python `uuid` module: https://docs.python.org/3.13/library/uuid.html (checked 2026-10-21)
- uniqueidentifier (Transact-SQL): https://learn.microsoft.com/en-us/sql/t-sql/data-types/uniqueidentifier-transact-sql?view=azuresqldb-current (checked 2026-10-21)
- datetime2 (Transact-SQL): https://learn.microsoft.com/en-us/sql/t-sql/data-types/datetime2-transact-sql?view=azuresqldb-current (checked 2026-10-21)
- SYSUTCDATETIME (Transact-SQL): https://learn.microsoft.com/en-us/sql/t-sql/functions/sysutcdatetime-transact-sql?view=azuresqldb-current (checked 2026-10-21)
- Durable orchestrator code constraints (time and UUIDs in orchestrators): https://learn.microsoft.com/en-us/azure/durable-task/common/durable-task-code-constraints (checked 2026-10-21)
- Ruff `DTZ` rules (flake8-datetimez): https://docs.astral.sh/ruff/rules/#flake8-datetimez-dtz (checked 2026-10-21)
