# ADR 0030: Cosmos containers and partition keys

- **Status:** Accepted
- **Date:** 2026-10-21
- **Decided by:** Product owner and architect
- **Specified in:** 80-data-model.md, 13-conversations-and-memory.md, 12-chat-pipeline.md, 83-infrastructure.md, 82-security.md, 84-testing.md
- **Related ADRs:** 0015, 0024, 0032, 0033, 0058, 0071, 0073, 0077, 0078, 0102, 0143, 0148, 0151

## Context

Conversations live in Cosmos DB, partitioned by user, with per-user access control; they are kept indefinitely and encrypted at rest with service-managed keys. Three containers hold the user-owned data: `conversations`, with a hierarchical key of user object ID then session ID (a session document and one document per turn); `memories`, keyed by user object ID (one document per user); and `usage`, keyed by user object ID (token counters and short-lived rate-limit windows with per-item TTL). Every read and write takes its partition key from the caller's validated token, so cross-user access is impossible by construction. Retention is the container's default TTL, off by default; continuous backup is on; `dev` is serverless and `prod` autoscale; key-based auth is disabled. Every Cosmos DB query on the chat path is single-partition.

The keys and queries must meet four needs:

- No query text may be assembled from input, because a user name, tag or session ID spliced into a filter lets one caller read another's history.
- A user is the immutable object ID, never an e-mail address or part of one, which different people share.
- A session ID is never chosen by the browser, because client-side numbers collide between tabs and users, and a shared number as a partition key mixes users' data.
- A session records its project, so history stays inside the project it belongs to and disappears when access to that project is revoked.

Vendor facts (checked 2026-10-14, 2026-10-15 and 2026-10-21):

- Hierarchical partition keys take up to 3 levels, are set at container creation (`kind: 'MultiHash'`, `version: 2`), need the full key for a point read, and serve a leading-prefix query as a targeted query over only the matching partitions.
- A transactional batch covers one full partition key, is all-or-nothing and holds at most 100 operations, 2 MB and 5 seconds of work; `_etag` with if-match gives `412` when stale.
- TTL: a container `defaultTtl` of `-1` means no default expiry, `n` means an item expires `n` seconds after its last write; an item's own `ttl` overrides it; expired items vanish from queries before background deletion.
- A logical partition holds at most 20 GB; an item is at most 2 MB and an `id` at most 1,023 bytes. Data-plane roles (Built-in Data Reader `00000000-0000-0000-0000-000000000001`, Built-in Data Contributor `00000000-0000-0000-0000-000000000002`) are separate from Azure RBAC and assignable at account, database or container scope.
- Session consistency honours read-your-writes within one client's session (session tokens are per partition and held by the client instance); strong and bounded-staleness reads use two replicas and cost twice the request units.
- Continuous backup has 7-day and 30-day tiers and restores to a new account; the async client is `azure.cosmos.aio` in `azure-cosmos` 4.17.1, whose `CosmosClient` takes `connection_timeout` (seconds).

## Decision

1. **Account and database.** One account per environment on the GA API `Microsoft.DocumentDB/databaseAccounts@2026-03-15`: default consistency `Session`; `disableLocalAuth: true`; `disableKeyBasedMetadataWriteAccess: true`; continuous backup `Continuous30Days` in `prod`, `Continuous7Days` in `dev` and `load`; zone-redundant in `prod` and `load` (ADR 0102); serverless (`EnableServerless`) in `dev` only; service-managed encryption at rest; full-text query logging left at its default, off. Database `docchat` with no shared throughput.
2. **The three containers.**

   | Container | Partition key | `defaultTtl` | Indexing policy | Autoscale maximum |
   |---|---|---|---|---|
   | `conversations` | `{paths: ['/userId', '/sessionId'], kind: 'MultiHash', version: 2}` | `-1` when `conversationRetentionDays` is 0 (default), otherwise days × 86,400 | consistent, automatic; `includedPaths` `/userId/?`, `/sessionId/?`, `/type/?`, `/projectId/?`, `/lastTurnAt/?`, `/seq/?`, `/status/?`, `/endedAt/?`, `/memoryExtraction/state/?`, `/retrieval/kept/[]/documentId/?`; `excludedPaths` `/*` and `/"_etag"/?`; composite index (`/lastTurnAt` ascending, `/sessionId` ascending) | `prod` 4,000 RU/s; `load` 30,000 RU/s |
   | `memories` | `{paths: ['/userId'], kind: 'Hash', version: 2}` | `-1` when `memoryRetentionDays` is 0 (default), otherwise days × 86,400 | consistent, automatic; no included path, `excludedPaths` `/*` (point reads only) | `prod` 1,000 RU/s; `load` 30,000 RU/s |
   | `usage` | `{paths: ['/userId'], kind: 'Hash', version: 2}` | `-1` (only items carrying `ttl` expire) | consistent, automatic; no included path, `excludedPaths` `/*` | `prod` 1,000 RU/s; `load` 30,000 RU/s |

   `dev` containers have no throughput options (serverless). No container has unique keys: every `id` is unique within its logical partition and derived or random (ADR 0015).
3. **What each container holds.** `conversations`: per session, in the logical partition `[userId, sessionId]`, one `session` document (`id` = session ID, carrying its `projectId`), one `turn` document per turn and one write-once `turn_trace` per finalised turn (ADR 0071). `memories`: one `memory` document per user, `id` = `userId`. `usage`: counters and windows of at most 1 KB each with an explicit `ttl`: day counters (40 days), month counters (400 days), chat and download rate windows (180 seconds), write-rate windows (180 seconds), stream leases (`chat.max_turn_seconds` + 30 seconds) and download leases (300 seconds) (ADRs 0058, 0143, 0148). Every document type is a Pydantic model in `docchat_core.cosmos` with camelCase fields.
4. **Keys come only from the token.** The `userId` value of every read, write and query is `str(principal.oid)` of the validated token, lower-case hyphenated. No request field, header, path segment or cursor supplies it, admins included. A `sessionId` from a path or body is only ever combined with the caller's own `oid`, so a guessed session ID resolves inside the guesser's own partition, where it doesn't exist.
5. **Access patterns.** Chat-path operations (`createChatTurn` and its after-response work, `getCitation`, `getCitationContent`, `getCitationDocument`, `getSession`, `putTurnFeedback`) use point reads, single-partition queries on the full key `[oid, sessionId]`, transactional batches and patches. Two application queries use the prefix `partition_key=[oid]`, with `c.userId = @userId` also in the `WHERE` clause: `listSessions`, filtered to the projects the caller can reach at query time (ADR 0078), and the memory-extraction re-check `ensure_post_turn_work`, which runs off the request path after a turn (13-conversations-and-memory.md section 6.5). Outside the app, the operator export and deletion commands read one user's partitions by that user's prefix (ADR 0033), and only `docchat-ops user redact-document` queries across partitions.
6. **Every query is parameterised.** Each Cosmos DB query is constant text with `@` parameters; `type`, `status` and state literals travel as parameters too; no query text is ever built from input.
7. **Project reachability.** A session, turn or citation is served only while its stored `projectId` is reachable for the caller now: membership is re-read from SQL on every request, so revoked access hides the history at once.
8. **Client and identities.** `docchat_core.clients` builds the API's one async `CosmosClient` with `ManagedIdentityCredential` and `connection_timeout=5`; per-operation timeouts are 2 seconds for a point read and 5 seconds for a query page, batch, patch, replace or create. Session tokens aren't shared between API instances, so the store never assumes read-your-writes across requests and settles a `409`, `412` or not-found met right after a write by re-reading within its bounded retries. The API identity holds Cosmos DB Built-in Data Contributor at `/dbs/docchat`; the ingestion identity has no Cosmos DB access; people reach data only through the just-in-time groups (ADR 0024).

## Alternatives considered

- **A single key `/userId` for `conversations`.** Rejected: a heavy user's whole history would sit in one logical partition, bounded at 20 GB, and every session read would filter inside it; the second level keeps each session's documents together for transactional batches and point reads.
- **Session ID as the only key.** Rejected: listing a user's sessions would become a cross-partition query over every user's data, and the user boundary would rest on a filter, not on the key.
- **A synthetic key such as `"{oid}|{sessionId}"`.** Rejected: hierarchical keys give the same co-location and also serve the user-prefix query that lists sessions without a cross-partition fan-out.
- **Strong or bounded-staleness consistency.** Rejected: every read would cost twice the request units, and with one region and one write location they add nothing the flows need; ETag conditions and bounded re-reads give that correctness under Session.
- **Shared database throughput.** Rejected: one container's burst (`conversations` at turn finalisation) would take capacity from `usage`, whose limit checks sit before every model call.
- **Default indexing of every path.** Rejected: write cost grows with every indexed property of a 100 KB trace; each container indexes only the paths its queries filter or sort on, and `memories` and `usage` index nothing.
- **Partition keys from request fields, guarded by checks.** Rejected: a missed check would expose another user's data; taking the key only from the token makes cross-user access impossible by construction.

## Consequences

- A user can never read or write another user's conversations, memory or counters through the app; operators reach them only under just-in-time access, which data-plane logs record.
- Each container's indexing policy is part of its definition: a new filter or sort path needs an indexing-policy change in the template before the query ships.
- Throughput: the `prod` maxima give about 13 turns per second at about 300 RU per completed turn before throttling, and autoscale bills at least 10% of each maximum every hour (400, 100 and 100 RU/s while idle); the load test measures the real charge per turn (ADR 0151).
- Retention stays a parameter: setting `conversationRetentionDays` or `memoryRetentionDays` above 0 starts expiry counted from each item's last write (ADR 0032).
- The partition key can't change after creation; a new key means a new container and a copy, so the key paths stay the same for the life of the deployment.
- Tests: integration tests of the chat path run with a recording wrapper on the container clients and fail on any call without a partition key, with only a prefix of the hierarchical key or with cross-partition querying enabled, `listSessions`' prefix query excepted; the isolation registry proves that another user's session ID answers `404`; emulator tests need an image with working hierarchical keys.

## References

- Hierarchical partition keys: https://learn.microsoft.com/en-us/azure/cosmos-db/hierarchical-partition-keys (checked 2026-10-14)
- Transactional batch operations: https://learn.microsoft.com/en-us/azure/cosmos-db/transactional-batch (checked 2026-10-14)
- Time to live: https://learn.microsoft.com/en-us/azure/cosmos-db/time-to-live (checked 2026-10-14)
- Indexing policies (array paths with `/[]`): https://learn.microsoft.com/en-us/azure/cosmos-db/index-policy (checked 2026-10-15)
- Consistency levels (Session guarantees; read cost of strong and bounded staleness): https://learn.microsoft.com/en-us/azure/cosmos-db/consistency-levels (checked 2026-10-21)
- Service limits (20 GB per logical partition, 2 MB items): https://learn.microsoft.com/en-us/azure/cosmos-db/concepts-limits (checked 2026-10-14)
- Data-plane role-based access control: https://learn.microsoft.com/en-us/azure/cosmos-db/how-to-connect-role-based-access-control (checked 2026-10-14)
- `azure.cosmos.aio.CosmosClient` (`connection_timeout`): https://learn.microsoft.com/en-us/python/api/azure-cosmos/azure.cosmos.aio.cosmosclient (checked 2026-10-15)
- Reliability in Azure Cosmos DB (zone redundancy under autoscale): https://learn.microsoft.com/en-us/azure/reliability/reliability-cosmos-db (checked 2026-10-14)
