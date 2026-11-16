# ADR 0078: Session list and session reads

- **Status:** Accepted
- **Date:** 2026-10-18
- **Decided by:** Architect
- **Specified in:** 13-conversations-and-memory.md, 11-api.md, 80-data-model.md, 83-infrastructure.md, 10-web-client.md, 82-security.md, 84-testing.md, 85-observability.md
- **Related ADRs:** 0012, 0013, 0020, 0030, 0033, 0056, 0064, 0071, 0072, 0079, 0080, 0084, 0090, 0143

## Context

- Users need to find a conversation from days ago, see it exactly as it was answered and continue it. The chat screen lists the user's sessions newest first, paged. Reading a session returns, for each turn, the question, the answer, citations, follow-ups, feedback and timestamps; the standalone query, the retrieved text and model details stay on the server.
- Access follows projects. A session in a project the caller can't reach now is hidden and answers `404` while access is revoked, and it reappears unchanged when access returns; the list filters by reachable projects when it runs; operator export and deletion still include hidden sessions. Every operation answers `404` for a session or turn the caller can't reach, so it never confirms that one exists, and isolation tests cover every session path.
- The list must be a server query of the caller's own data, sorted on a field the API returns and paged, so that its cost and its response size stay flat however long the user's history grows, and a new conversation never shares storage with an earlier one.
- Layout (ADR 0030, ADR 0071): `conversations` is partitioned by `[userId, sessionId]`; the session document holds `projectId` (set once at creation), `title`, `createdAt`, `lastTurnAt` and `turnCount`; turn documents carry a contiguous `seq` from 1.
- Vendor facts: a query on the leading key `[userId]` is a targeted cross-partition query that reaches only the physical partitions holding that prefix, and the async SDK routes `query_items(partition_key=[userId])` through the prefix's range; an `ORDER BY` on two or more properties requires a composite index, which also serves the opposite order on all paths; `TOP` accepts a parameter; a request is at most 2 MB and a response 4 MB.
- Reachability is read from SQL on every request (ADR 0020): a project is reachable when it is `active` and the caller is `Admin` or has a membership row; archived projects are unreachable on every user path, admins included.

## Decision

**`listSessions` — `GET /v1/sessions`.** Query: `limit` 1 to 50 (default 20), `cursor` 1 to 512 characters, `projectId` (UUID), all optional. `200` `SessionPage` = `{"items": [SessionSummary], "nextCursor": string | null}`, `SessionSummary` = `sessionId`, `projectId`, `title`, `createdAt`, `lastTurnAt`, `turnCount`, ordered by `lastTurnAt` descending (the server time at which the session's newest turn started), ties by `sessionId` descending.

1. `reachable` = `list_reachable_project_ids(principal)`. With `projectId`: not in `reachable` → `404 project_not_found`, else the filter is `[projectId]`. Without: the filter is `reachable`, sorted. An empty filter answers `{"items": [], "nextCursor": null}` with no Cosmos DB call.
2. One prefix query with `partition_key=[oid]`, `@type` = `session`, `@take` = `limit` + 1. With a cursor, a second constant query text adds `AND (c.lastTurnAt < @t OR (c.lastTurnAt = @t AND c.sessionId < @s))` after the `ARRAY_CONTAINS` line; no query text is ever built from strings.

```sql
SELECT TOP @take c.sessionId, c.projectId, c.title, c.createdAt, c.lastTurnAt, c.turnCount
FROM c
WHERE c.userId = @userId AND c.type = @type
  AND ARRAY_CONTAINS(@projectIds, c.projectId)
ORDER BY c.lastTurnAt DESC, c.sessionId DESC
```

3. More than `limit` rows → `nextCursor` from the `limit`-th row; the first `limit` rows are returned.
4. Cursor: base64url without padding of the UTF-8 JSON `{"v": 1, "t": "<lastTurnAt of the last item>", "s": "<sessionId of the last item>", "p": "<projectId filter or null>"}`, parsed by a Pydantic model with `extra="forbid"`. Undecodable, invalid, another version, or `p` different from the request's `projectId` → `422 validation_failed`, reason `invalid_cursor`.
5. Indexing (`conversations`, created by Bicep): the included paths contain `/userId/?`, `/sessionId/?`, `/type/?`, `/projectId/?` and `/lastTurnAt/?`, and the composite index `[{"path": "/lastTurnAt", "order": "ascending"}, {"path": "/sessionId", "order": "ascending"}]` serves the descending order.

Errors: `401`, `403`, `404 project_not_found`, `422` (`out_of_range`, `invalid_cursor`, `invalid_uuid`), `429 too_many_requests`, `503 dependency_unavailable`.

**`getSession` — `GET /v1/sessions/{sessionId}`.** Query `turnsLimit` 1 to 50 (default 20). Point-read the session in `[oid, sessionId]`, then `authorize_project_for_chat(principal, projectId)`; a missing session and an unreachable project both answer `404 session_not_found` with one body. `200` `SessionDetail` = `sessionId`, `projectId`, `title`, `createdAt`, `lastTurnAt`, `turnCount`, `turns` (the newest `turnsLimit` turns as `TurnView`, ascending `seq`), `earlierTurnsCursor` (string or null).

**`listSessionTurns` — `GET /v1/sessions/{sessionId}/turns`.** Same authorisation and `404`. Query `limit` 1 to 50 (default 20), `cursor` 1 to 512 characters (absent = the newest page). `200` `TurnPage` = `{"items": [TurnView], "nextCursor": string | null}`, ascending `seq` within the page; `nextCursor` leads to the next older page and is null once the oldest turn is included. Cursor: base64url without padding of `{"v": 1, "b": <seq>}`; the page holds turns with `seq < b`; `b` must be an integer from 2 to `turnCount + 1`, otherwise `422 invalid_cursor`.

Both read turns with one single-partition query, `@type` = `turn`, `@before` = the cursor's `b` or `turnCount + 1`, `@take` = the limit + 1:

```sql
SELECT TOP @take c.id, c.seq, c.status, c.question, c.answer, c.answerTruncated, c.error,
       c.citations, c.allowedLinks, c.followups, c.feedback, c.createdAt, c.endedAt, c.deadlineAt
FROM c
WHERE c.userId = @userId AND c.sessionId = @sessionId AND c.type = @type AND c.seq < @before
ORDER BY c.seq DESC
```

The first `limit` rows are reversed to ascending order; more rows → the cursor `{"v": 1, "b": <smallest seq returned>}`.

**`TurnView`** has exactly 13 keys: `turnId`, `seq`, `status` (`streaming`, `completed`, `failed`, `cancelled`), `question`, `answer` (with citation tokens `⟦n⟧`; empty when nothing was released), `answerTruncated`, `errorCode` (the stored `error.code`, or null), `citations` (`CitationView`: `number`, `title`, `pageNumber`, `headingPath`, `contentType`, `openAs`, the shape of the stream's `citations` event), `allowedLinks`, `followups`, `feedback` (`rating`, `category`, `comment`, `updatedAt`, or null), `createdAt`, `endedAt`. A turn stored as `streaming` whose `deadlineAt` has passed reads as `failed` with `errorCode` `turn_abandoned`. Nothing else leaves the server: no document, version, chunk-set or index IDs, chunk keys, standalone or search query, retrieval results or scores, trace, model input, deployments, usage, timings, broadened flag, `finishReason`, memory-extraction state, `clientTurnId`, `traceId` or `deadlineAt`. `status`, `errorCode` and `allowedLinks` are part of the view because the client needs them to render a stored turn exactly as it rendered the live one (links only for `allowedLinks`, ADR 0084; partial and failed answers marked), and none of them reveals retrieved text or model details.

**Hidden sessions.** A session whose `projectId` the caller can't reach now is absent from `listSessions`; `getSession` and `listSessionTurns` answer `404 session_not_found`, `putTurnFeedback` `404 turn_not_found`, and `createChatTurn` refuses it through the chat pipeline's project check. Nothing is written or deleted, so the session reappears unchanged when access returns. Operator export and deletion (ADR 0033) include hidden sessions. Admins get no wider access: every read is in the caller's own partition.

**Client.** The session panel requests `listSessions` with `limit=30`, loads more through `nextCursor` and de-duplicates by `sessionId` (a session that gets a new turn while the user pages moves to the top and can appear twice). Opening a session calls `getSession` with `turnsLimit=30`, and the control for earlier messages calls `listSessionTurns` with `limit=30`; a `404` shows the not-available state and refetches the list. A newest turn stored as `streaming` that this tab isn't streaming is polled through `getSession` every 5,000 ms while the tab is visible, for at most 250 seconds (the 220-second longest turn cap plus 30 seconds), after which it reads as `failed` or the client offers a manual refresh.

## Alternatives considered

- **OFFSET and LIMIT paging.** Rejected: the cost grows with the offset, and pages shift when sessions get new turns; a keyset page costs the same at any depth.
- **SDK continuation tokens as cursors.** Rejected: opaque SDK state that the API can't validate or bind to the `projectId` filter, carried through every page of a cross-partition prefix query; the keyset cursor is at most 512 characters, validated by a model and bound to the filter.
- **Filtering hidden sessions after the query.** Rejected: pages could come back short or empty while more sessions exist; filtering inside the query keeps pages full.
- **Marking sessions hidden when access changes.** Rejected: every membership change, archive or restore would need writes across the affected users' sessions, racing new turns; filtering at query time writes nothing.
- **One per-user index document listing sessions.** Rejected: one hot item updated by every turn under ETag contention, bounded by the 2 MB item limit.
- **Every turn in `getSession`.** Rejected: a session holds up to 500 turns (`conversations.max_turns_per_session`) with answers up to 100,000 code points; paging bounds a response to 50 turns.
- **Ordering by `createdAt`.** Rejected: a conversation the user just continued would stay buried; `lastTurnAt` puts it at the top.
- **`403` for hidden sessions.** Rejected: it confirms the session exists; `404` doesn't.

## Consequences

- Each list costs one SQL reachability query and one Cosmos DB prefix query. The parameter list holds every reachable project ID, about 40 bytes each: about 80 KB at the 2,000-project scale the tests use, far below the 2 MB request limit, which a list of about 50,000 IDs would reach.
- Prefix queries run only in `listSessions` and the post-turn work; every chat-path call names the full key.
- The Cosmos DB emulator has no composite indexes, so integration tests can't catch a missing one; the load test's `listSessions` measurement runs against the real service, where the two-property `ORDER BY` fails without it.
- Another sort order needs its composite index added, and the index transformation finished, before the release that queries with it.
- Hidden sessions stay stored, keep their retention clock and appear in exports.
- Monitoring: spans `docchat.sessions.list` (page limit, rows returned, reachable project count, filter flag, request charge), `docchat.sessions.get` and `docchat.sessions.list_turns`; `docchat.conversations.request_charge` by `operation` (`list_sessions`, `get_session`, `list_turns`).
- Tests: cursors round-trip, and garbage, tampered, oversized, wrong-version and wrong-filter cursors answer `422 invalid_cursor`; `TurnView` serialises exactly its 13 keys, and an expired `streaming` turn maps to `failed` / `turn_abandoned`; the list is newest first over pages and honours `projectId`; removing a membership hides the session from the list and makes its reads and feedback answer `404`, and restoring it brings everything back unchanged; another user, an `Admin` included, gets `404` with the caller's session and turn IDs; authorisation matrix rows for the three operations; a recording wrapper proves every chat-path call names the full key; `listSessions` keeps p95 at or below 200 ms for a user with 500 sessions while 100 answer streams run; after 3 questions and a page reload the chat shows 3 turns with citations, feedback state and follow-ups on the newest turn only.

## References

- Hierarchical partition keys and prefix queries: https://learn.microsoft.com/en-us/azure/cosmos-db/hierarchical-partition-keys (checked 2026-10-14)
- Async client prefix routing: https://github.com/Azure/azure-sdk-for-python/blob/main/sdk/cosmos/azure-cosmos/azure/cosmos/aio/_cosmos_client_connection_async.py (checked 2026-10-14)
- Indexing policies, composite indexes for `ORDER BY` on two or more properties, opposite order on all paths, index transformation: https://learn.microsoft.com/en-us/azure/cosmos-db/index-policy (page updated 2026-09-21, checked 2026-10-18)
- `TOP` with a parameter: https://learn.microsoft.com/en-us/cosmos-db/query/top (checked 2026-10-15)
- Service limits (2 MB request, 4 MB response, 2 MB item): https://learn.microsoft.com/en-us/azure/cosmos-db/concepts-limits (checked 2026-10-14)
- Linux emulator feature support (no composite indexes): https://learn.microsoft.com/en-us/azure/cosmos-db/emulator-linux (checked 2026-10-14)
