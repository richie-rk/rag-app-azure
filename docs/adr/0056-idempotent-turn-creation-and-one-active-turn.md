# ADR 0056: Idempotent turn creation and one active turn

- **Status:** Accepted
- **Date:** 2026-10-18
- **Decided by:** Architect
- **Specified in:** 11-api.md, 12-chat-pipeline.md, 13-conversations-and-memory.md, 80-data-model.md, 81-config.md, 10-web-client.md, 84-testing.md
- **Related ADRs:** 0007, 0012, 0015, 0030, 0055, 0057, 0058, 0070, 0071, 0072, 0073, 0078

## Context

- The chat request (`createChatTurn`) carries `projectId`, `sessionId` (absent for a new session), `clientTurnId` (a UUID the client draws for each question it sends) and `question`, and nothing else. The server allocates every session ID and returns it in the stream's `meta` event, so two tabs never share a session and session identity never depends on client code. A retry sends a new `clientTurnId`; a failed or cancelled turn never reaches the model as history; a session belongs to one project, and a request that pairs it with another project gets `404`.
- Resubmits are normal traffic: a user presses Send twice, a tab reloads mid-request, a network path resends a `POST` after a dropped connection. A turn spends model capacity (about 20,000 tokens across its calls), so a resubmit must never start a second model call or a second charge, and it should get the same answer back.
- Every conversation document is stored, listed and loaded under one key: the caller's Entra object ID, then the session ID (ADR 0030). A session and its turns therefore share one logical partition.
- Two tabs can send into one session at the same moment, and an instance can die mid-turn while no instance keeps state between requests. Any guard must live in the shared store and expire on its own.
- Vendor facts (Azure Cosmos DB, checked 2026-10-14): a transactional batch commits point operations on one logical partition all-or-nothing and in order, at most 100 operations and 2 MB; a create of an existing `id` fails with `409` and the batch's other operations with `424`; `if-match` on `_etag` fails with `412` when the item changed. The account runs Session consistency and API instances don't share session tokens, so no request may assume it reads another instance's latest write.

## Decision

**Identifiers.** Session and turn IDs are derived from the request as UUIDv5 over constant namespaces held in `docchat_core.cosmos.conversations.ids`, which never change. Names are UTF-8; every UUID inside a name is canonical lower-case hyphenated.

| ID | Namespace | Name |
|---|---|---|
| New session | `60191fe1-b79c-4c47-995d-e0a0e20cb164` | `"{oid}:{clientTurnId}"` |
| Turn | `fc6c167c-bdc0-407d-9fcb-0bb4c6a2c701` | `"{sessionId}:{clientTurnId}"` |
| Turn trace | `fc6c167c-bdc0-407d-9fcb-0bb4c6a2c701` | `"{turnId}:trace"` |

**Creating the turn.** Pre-stream step P8 is `ConversationStore.begin_turn(principal, *, project_id, session_id, client_turn_id, question, received_at, trace_id)` in `docchat_api.conversations.store`. It runs after the rate, quota and lease checks (ADR 0058) and after project and index resolution; `received_at` is the time the route received the request. It returns `Started(handle, session, is_new_session)` or `Replay(turn)`, or raises `ConversationError(code, status, retry_after_seconds, session_id, turn_id)`, which the route answers as a problem response before any `200`.

1. Derive the IDs and point-read the derived turn ID in `[oid, sessionId]`. A hit is a duplicate (below); a failed read is `503 dependency_unavailable`.
2. New session (no `sessionId`): one batch `[create session, create turn]`, the session with `turnCount` 1, `activeTurn` set and its title, the turn with `seq` 1 and status `streaming`. A `409` on either create means a concurrent identical request won: re-read the turn up to 3 times (waits of 25, 50 and 100 ms) and treat it as a duplicate; still missing → `503 dependency_unavailable`.
3. Existing session: point-read it. Missing, or `projectId` different from the request's → `404 session_not_found`, with one body for both cases. `turnCount` at or above `conversations.max_turns_per_session` (ops_config, default 500, range 50 to 5,000) → `409 session_turn_limit`. A live `activeTurn` → `409 turn_in_progress`. Otherwise one batch: replace the session with `if_match` set to the ETag just read (`turnCount + 1`, `lastTurnAt`, `activeTurn`, `updatedAt`); create the turn (`seq` = the new `turnCount`, status `streaming`); and, when an expired `activeTurn` names a turn that is still `streaming`, replace that turn with status `failed`, `error` `{code: "turn_abandoned", retryable: true}` and `endedAt`, under its own ETag. The metric `docchat.conversations.abandoned` counts each takeover after the commit.
4. A `409` on the turn create is a concurrent duplicate. A `412` repeats from step 3 with fresh reads, at most 3 attempts (waits of 25, 50 and 100 ms); then `409 turn_in_progress` when the last read shows a live `activeTurn`, otherwise `503 dependency_unavailable` (`retryAfterSeconds` 5). Throttling past the store's retry limit, a timeout or a `5xx` is `503 dependency_unavailable`.
5. `seq` starts at 1, is contiguous per session and is never reused; failed and cancelled turns keep their numbers.

**One active turn per session.** The session's `activeTurn` is `{turnId, deadlineAt}` or `null`. `deadlineAt` = `received_at` + `chat.max_turn_seconds` (ops_config, default 180 s, range 60 to 220 s) + 30 s, so 210 s after receipt by default; the turn stores the same value, and no response ever returns it. A new turn starts only when `activeTurn` is `null` or past its `deadlineAt`; otherwise the answer is `409 turn_in_progress` with `retryAfterSeconds` = the whole seconds until `deadlineAt`, rounded up, at least 1. Finalisation clears `activeTurn` when it still names the turn (ADR 0057). The 30 s cover the 15-second shielded finaliser that ends every turn by the cap, with 15 s to spare, and the stream lease expires at its acquisition plus the same cap plus 30 s (ADR 0058), so a lease never expires while its session is still blocked.

**Duplicate `clientTurnId`.** A duplicate is found by the point-read or by a `409` on the create. The stored turn's `question` (exact string equality with the normalised request question) and `projectId` are compared first.

| Stored turn | Same question and project | Answer |
|---|---|---|
| any status | no | `409 client_turn_id_conflict` |
| `completed` | yes | `200` replay |
| `streaming`, `deadlineAt` in the future | yes | `409 turn_in_progress` with `retryAfterSeconds` |
| `streaming`, `deadlineAt` passed | yes | store it as `failed` / `turn_abandoned` under its ETag (on `412`, re-read once and keep whatever terminal status it has), clear the session's `activeTurn` when it names this turn (`if_match`; a `412` is ignored, because the next start takes over), then `409 turn_already_ended` |
| `failed` or `cancelled` | yes | `409 turn_already_ended` |

Every `409` from `begin_turn` carries the extension member `sessionId`, and every one except `session_turn_limit` also carries `turnId`, because the turn limit refuses before any turn exists. A client that lost the `meta` event can reopen the session from these members.

**Replay.** A completed duplicate gets the `200` stream shape built from the stored turn: `meta` (`sessionCreated: false`, `replayed: true`, this request's `correlationId`); the stored answer as `delta` events of at most 2,000 characters each, cut on code-point boundaries and never inside a citation token; `citations` with the stored public fields and `allowedLinks`; `followups` when stored; `done` with `usage` projected from the stored `usage.totals` and `truncated` = the stored `answerTruncated`. It makes no model, search or Content Safety call, no usage accounting and no write. The replay counts toward the request rate and holds a stream lease until its `citations` event.

**Abandoned turns.** A turn still `streaming` after its `deadlineAt` belongs to a process that died. It is never history; session reads show it as `failed` with `errorCode` `turn_abandoned`, and a takeover or a duplicate stores that status. `turn_abandoned` (retryable) is a stored code only, registered in `docchat_core.errors.CODES` with no HTTP status and never written as a stream event; a turn that its own process ends at shutdown is stored with `service_restarting` instead (ADR 0007).

**The web client.** Every send, retries included, carries a new `crypto.randomUUID()` as `clientTurnId`; a new chat omits `sessionId` and takes the session ID from `meta`. On `409 turn_in_progress` less than 10 s after the tab's previous turn in that session reached `citations`, the client resends the identical request (same `clientTurnId`) after 1,000 ms, at most 2 times, which covers the replica lag that Session consistency can leave; otherwise it shows the wait from `retryAfterSeconds`.

## Alternatives considered

- **An `Idempotency-Key` header with a server-side key table.** It needs another container or table, a lookup before every turn and its own expiry. With derived IDs the create itself is the uniqueness check.
- **Session IDs drawn by the client.** A client could reuse one ID across tabs or sessions, and session identity would depend on every client getting it right. The server allocates the ID and returns it in `meta`.
- **Random turn IDs plus a Cosmos DB unique key on `clientTurnId`.** A unique key holds only inside one logical partition, so two identical new-session requests would each create their own session. Deriving the session ID from `(oid, clientTurnId)` covers new sessions as well.
- **An active-turn lock renewed by heartbeat writes.** It adds a write every few seconds per stream and its own failure modes. The deadline bounds the block a dead process leaves at the cap plus 30 s with no write at all.
- **Several turns in flight per session.** History and sequence numbers would interleave. Parallel questions remain possible in separate sessions, since ADR 0058 allows 2 streams per user.
- **`409` for a completed duplicate.** The client would have to reload the session to see an answer that was already generated and charged; the replay returns it at no cost.

## Consequences

- A resubmit never creates a second turn, model call or charge, and no lookup table or sweeper exists. "The turn exists" and "the session counts it" commit together or not at all.
- A crashed instance blocks its session for at most `chat.max_turn_seconds` + 30 s (210 s by default, 250 s at the 220-second maximum); the next start takes over and marks the stale turn.
- Derived IDs are predictable from the `oid` and the `clientTurnId`. They aren't secrets: every read and write still takes its partition key from the validated token.
- A client that reuses a `clientTurnId` for another question gets `409 client_turn_id_conflict`, never another question's answer.
- The namespace constants must never change; a change would let a resubmit that straddles a deploy create a second turn.
- Obligations: `conversations.max_turns_per_session` and `chat.max_turn_seconds` as typed ops_config rows; the metric `docchat.conversations.abandoned`; tests proving that derived IDs match known vectors and ignore input case; that a duplicate of a completed turn replays identical text, citations and usage with no model call and no write; that duplicates of streaming, failed and cancelled turns and of a different question get their `409` codes with `sessionId` and `turnId`; that 10 concurrent identical first requests create one session and one turn; that two concurrent starts in one session admit exactly one with no gap in `seq`; that a turn past its deadline is taken over and stored `turn_abandoned`; that finalisation after a takeover leaves the turn `turn_abandoned`; and a browser test of the bounded resend.

## References

- Azure Cosmos DB transactional batch: https://learn.microsoft.com/en-us/azure/cosmos-db/transactional-batch (checked 2026-10-14)
- Azure Cosmos DB transactions and optimistic concurrency: https://learn.microsoft.com/en-us/azure/cosmos-db/database-transactions-optimistic-concurrency (checked 2026-10-14)
- Azure Cosmos DB hierarchical partition keys: https://learn.microsoft.com/en-us/azure/cosmos-db/hierarchical-partition-keys (checked 2026-10-14)
- Azure Cosmos DB service limits: https://learn.microsoft.com/en-us/azure/cosmos-db/concepts-limits (checked 2026-10-14)
- Python `uuid.uuid5`: https://docs.python.org/3.13/library/uuid.html (checked 2026-10-18)
