# ADR 0073: Conversation store contract

- **Status:** Accepted
- **Date:** 2026-10-18
- **Decided by:** Architect
- **Specified in:** 13-conversations-and-memory.md, 12-chat-pipeline.md, 01-repository.md, 84-testing.md
- **Related ADRs:** 0006, 0012, 0013, 0055, 0056, 0057, 0063, 0065, 0069, 0071, 0072, 0074, 0075, 0076, 0081, 0113, 0131

## Context

Two parts of the API build the chat path. The chat pipeline decides when, during `createChatTurn`, each conversation read or write happens and what values it produces; the conversation store decides what is stored and enforces every rule about it: deterministic IDs, sequence numbers, the one-active-turn guard, statuses, duplicate submits, sizes and the selection of history. The API loads the session from the caller's own partition, persists each turn with its complete diagnostic record, adds follow-ups to the saved turn and runs the summary and memory work after the response.

Each turn must record which chunks, prompt, deployment and token usage produced its answer, in the same write that stores the turn (ADR 0071), so the boundary has to carry every one of those values into finalisation. The codebase keeps deep modules with small interfaces and a Pydantic model at every boundary, Cosmos DB documents included, with unknown input fields rejected, so that no field is ignored or misnamed silently. Both sides therefore need one set of names for stored citations, usage and trace fields, one citation-token strip helper in `docchat_core`, and one rule for which `409` problems carry `sessionId` and `turnId`.

## Decision

1. **One store object.** `docchat_api.conversations.store.ConversationStore` is created once per API worker at start-up with the `azure.cosmos.aio.CosmosClient` that `docchat_core.clients` builds (`connection_timeout=5`), and closed at shutdown. Every method is `async`. Every read, write and query uses `principal.oid` as the first partition-key level. Per-operation timeouts are 2 s for a point read, 5 s for a query page and 5 s for a batch, patch, replace or create; on the chat path the SDK retries throttling (`429`) for at most 3 attempts and 5 s in total, after which the method fails as item 2 states.
2. **Errors.** Failures raise `ConversationError(code, status, retry_after_seconds=None, session_id=None, turn_id=None)`. A store that can't be reached, times out or stays throttled raises `dependency_unavailable` (503) from `begin_turn`, which the route maps to a problem response before the stream starts; after the stream starts, `read_recent_turns` raises `conversation_unavailable` and `finalize_turn` raises `turn_save_failed`, which the chat pipeline writes as its `error` event. Every `409` from `begin_turn` carries `session_id`; all except `session_turn_limit` also carry `turn_id`, because that limit refuses before a turn exists.
3. **The methods, and nothing else, that the chat pipeline calls:**

| Method | Contract |
|---|---|
| `begin_turn(principal, *, project_id, session_id, client_turn_id, question, received_at, trace_id) -> TurnStart` | Runs after the access, rate, quota and concurrency checks and before `meta`: session load and project check, derived IDs, duplicate detection, active-turn guard and the creation batch (ADR 0056). Returns `Started(handle, session, is_new_session)` or `Replay(turn)`. Raises `session_not_found` (404), `turn_in_progress` (409, with `retry_after_seconds`), `turn_already_ended` (409), `client_turn_id_conflict` (409), `session_turn_limit` (409) or `dependency_unavailable` (503). `TurnHandle` = `{oid, session_id, turn_id, seq, project_id, turn_etag, session_etag, deadline_at}` |
| `read_recent_turns(handle, max_tokens) -> list[HistoryTurn]` | Every `completed` turn with `seq` above `summary.coversThroughSeq`, oldest first, no turn-count limit, from one single-partition query read newest first in pages of 20 that stops after the turn at which the running `historyTokens` total first exceeds `max_tokens` (that turn kept). `HistoryTurn` = `{seq, question, answer, followups}`, the answer passed through `docchat_core.chat.citations.strip_citation_tokens`. Raises `conversation_unavailable` |
| `get_prompt_memory(oid) -> list[str]` | The memory lines for the preferences block (ADR 0076). Never raises: a failed read returns `[]` and logs `memory_unavailable` |
| `ensure_post_turn_work(session) -> None` | The turn-start re-check (ADR 0081). Returns at once and performs no I/O; may schedule the post-turn task |
| `finalize_turn(handle, outcome) -> FinalizeResult` | The finalisation batch (ADR 0072), awaited under the chat pipeline's shielded 15-second finaliser. `TurnOutcome` carries `status`, `answer`, `answer_truncated`, `finish_reason`, `error`, `citations`, `allowed_links`, `usage`, `question_suggested` and `trace: TurnTraceData` (every `turn_trace` field, `promptBudget` included). `FinalizeResult` = `{saved: bool, turn_etag}` |
| `attach_followups(handle, followups, usage_record) -> bool` | Patches `followups`, `followupsAt`, `usage` and `updatedAt` without an ETag, at most 3 attempts; returns `True` only when the patch committed |
| `get_turn(principal, session_id, turn_id) -> TurnDocument or None` | Point read in `[oid, session_id]` for `getCitation`, `getCitationContent` and `getCitationDocument`, which re-check project access themselves |
| `schedule_post_turn_work(oid, session_id, turn_id) -> None` | Registers one post-turn task (ADR 0081) and returns at once; the task never raises |

4. **Call order on the chat path.** Pre-stream step P8 calls `begin_turn`. After `meta`, step T2 runs `read_recent_turns(handle, max_tokens)` with `max_tokens` = `chat.context_token_budget` − `chat.answer_max_completion_tokens` (28,000 tokens by default), `get_prompt_memory(oid)` and `ensure_post_turn_work(session)` concurrently, then computes `question_suggested` with `docchat_core.memory.validators.is_suggested_question` (ADR 0075). Step T11 calls `finalize_turn` for a completed answer before the `citations` event; step T13 calls `attach_followups` only after `finalize_turn` returned `saved: true`, and writes the `followups` event only after it returns `True`; the shielded finaliser calls `finalize_turn` for `failed` and `cancelled` turns and `schedule_post_turn_work` for `completed` ones. The chat pipeline never writes a session or turn itself and reads the summary from `Started.session`.
5. **One set of field names.** The stored shapes are the conversation store's (ADR 0071): `StoredCitation` (`number`, `sourceId`, `chunkKey`, `searchIndexId`, `physicalIndexId`, `documentId`, `documentVersionId`, `chunkSetId`, `title`, `pageNumber`, `headingPath`, `contentType` as a MIME type, `openAs`), `TurnUsage` (`calls`, `totals`, `billable`) and the trace fields. The chat pipeline maps its values onto those names. The one citation shape that leaves the server, `CitationView` (`number`, `title`, `pageNumber`, `headingPath`, `contentType`, `openAs`), equals the chat pipeline's public `Citation` field for field.
6. **Boundary.** No other module reads or writes the `conversations` or `memories` containers. The conversation routes, the citation operations and the operator commands use the store functions and the document models in `docchat_core.cosmos`; the operator CLI builds on the `docchat_core.cosmos` repositories, because it doesn't import `docchat_api`.

## Alternatives considered

- **The chat pipeline writes Cosmos DB documents itself, sharing only the models.** Lost: two modules would enforce sequence, duplicate and status rules, and one path forgetting an ETag condition would corrupt a session.
- **A generic repository (read, upsert, patch an item).** Lost: a shallow interface pushes the turn lifecycle into the caller, against the deep-module rule, and gives no single place to test it.
- **An HTTP or queue boundary between chat and conversations.** Lost: both run in the same API process and deploy together; a network hop would add latency, a second authentication surface and failure modes to every turn.
- **Untyped dictionaries across the boundary.** Lost: typed models are what let the OpenAPI document, the stream schemas and the stored documents agree, and they reject unknown fields.

## Consequences

- Easier: each store rule has one implementation and one set of tests; the chat pipeline reads as a sequence of named steps; a new trace value needs one field in `TurnTraceData` and one in the document model.
- Harder: the contract is written in both 12-chat-pipeline.md and 13-conversations-and-memory.md, so any change edits both and their tests together.
- Obligations: the chat pipeline's integration tests run against this store over the Cosmos DB emulator, with no fake store; a unit test replays Cosmos DB responses to prove the `409` extension members (`session_turn_limit` with `session_id` and no `turn_id`; the other three with both); a recording wrapper on the Cosmos DB client proves that every call between `begin_turn` and the terminal event names the full `[oid, sessionId]` key and that prefix queries happen only in the session list and the post-turn work.
- Risk: every turn depends on this one module, so a defect in it reaches every conversation; the concurrency and duplicate tests of ADR 0056 and ADR 0072 run against it.
- Risk: the boundary rule is held by code review; the recording wrapper proves the partition key of every chat-path call, not which module made it.

## References

- Python async client `azure.cosmos.aio.CosmosClient` (`connection_timeout`, `timeout`): https://learn.microsoft.com/en-us/python/api/azure-cosmos/azure.cosmos.aio.cosmosclient
- `azure.cosmos.aio.ContainerProxy` (`read_item`, `query_items`, `execute_item_batch`, `patch_item` with `etag` and `match_condition`): https://learn.microsoft.com/en-us/python/api/azure-cosmos/azure.cosmos.aio.containerproxy
- Hierarchical partition keys and query routing: https://learn.microsoft.com/en-us/azure/cosmos-db/hierarchical-partition-keys
- Transactional batch: https://learn.microsoft.com/en-us/azure/cosmos-db/transactional-batch
