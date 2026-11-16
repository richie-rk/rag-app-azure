# ADR 0081: After-response work as tracked tasks

- **Status:** Accepted
- **Date:** 2026-10-21
- **Decided by:** Architect
- **Specified in:** 12-chat-pipeline.md, 13-conversations-and-memory.md, 84-testing.md, 85-observability.md
- **Related ADRs:** 0003, 0006, 0007, 0057, 0058, 0063, 0072, 0073, 0074, 0075

## Context

- Two jobs run after each answer: the rolling-summary rebuild when the unsummarised turns pass the threshold (ADR 0074) and memory extraction when memory is on (ADR 0075). Both are chat-small calls that take seconds (a summary call reads up to 12,000 tokens; an extraction attempt has a 20-second timeout). No turn may wait for them, the next turn re-checks both before it starts, and a crash may only delay them. Every instance is stateless, so the work can't depend on an instance surviving.
- The rolling summary is what keeps long conversations inside the prompt budget (ADR 0063), so rebuilds must actually happen, reliably, without slowing turns.
- The API drains at its stop signal: 90 s after SIGTERM it cancels what is still running, inside App Service's 120-second stop limit and Gunicorn's 110-second graceful timeout (ADR 0007).
- Python fact: the event loop keeps only weak references to tasks, so a task that nothing else references may be garbage-collected before it finishes; a background task needs a strong reference held somewhere.
- Only the turn task knows how a turn ended: a client can leave after the answer was finalised (the turn stays `completed`) or before it (the turn is `cancelled`), so the response's own lifetime can't decide whether the work runs (ADR 0057).

## Decision

1. **Scheduling.** `ConversationStore.schedule_post_turn_work(oid, session_id, turn_id)` creates one `asyncio.Task` and registers it with `docchat_api.chat.tasks.register_background_task(task)`, the process-wide registry that also holds the turn tasks with strong references until each ends (ADR 0057), and returns at once. The turn task's finaliser calls it once for each turn that ends `completed`: after `done` is written, or after a client disconnect that came once the turn was finalised. It is never called for `failed` or `cancelled` turns, and never from a response background task, so it runs whether or not the client is still connected.
2. **What the task runs**, in order: (a) extraction for this turn when its `memoryExtraction.state` is `pending`; (b) the pending re-check: one prefix query on the caller's `[oid]` partition prefix (`SELECT TOP 6 c.id, c.sessionId, c.endedAt, c.memoryExtraction.state AS state, c.memoryExtraction.leaseExpiresAt AS leaseExpiresAt FROM c WHERE c.userId = @userId AND c.type = @type AND ARRAY_CONTAINS(@states, c.memoryExtraction.state) ORDER BY c.endedAt ASC`, `@type` `turn`, `@states` `["pending", "running"]`), then for up to 5 results other than this turn: older than `memory.retry_window_hours` (default 24) → `expired`; `running` with an unexpired lease → skipped; otherwise extracted; (c) the summary rebuild when the session's summary is due. Each step runs in its own `try`: a failure is logged once (code and IDs) and the next step still runs. The task never raises; only cancellation propagates.
3. **The turn-start re-check.** The chat pipeline calls `ensure_post_turn_work(session)` when a turn starts. It returns at once and schedules the same task with no turn ID (steps (b) and (c) only) when the session's summary is due (`history.unsummarisedTokens` above `summary.trigger_tokens`, `summaryJob.leaseExpiresAt` null or past, `summaryJob.notBefore` null or past) or when this worker has started no re-check for the user in the last 60 s. The 60-second throttle is a per-worker map of at most 10,000 users; losing it changes only cost.
4. **Leases in Cosmos DB, no queue.** The summary rebuild claims `session.summaryJob.leaseExpiresAt` = now + `summary.lease_seconds` (default 120; 30 to 900 and at least 3 × `summary.timeout_seconds`) with a replace conditioned on the session's ETag. An extraction claims its turn by patching `memoryExtraction.state` to `running` and `leaseExpiresAt` = now + `memory.lease_seconds` (default 60; 30 to 900 and at least 3 × `memory.timeout_seconds`), conditioned on the turn's ETag. Two instances never run the same work, and any later re-check can reclaim an expired lease.
5. **Quotas.** Before each model call the work awaits `check_token_quota(oid)` (ADR 0058). `allowed` false, or `UsageStoreUnavailable` when the `usage` reads time out twice, defers the work with no model call and no failure or attempt counted; the span outcome is `deferred_quota`.
6. **Drain, restart and crash.** Post-turn tasks still running at the drain point (90 s after the stop signal) are cancelled. Work cut short by that cancellation, a restart or a crash keeps its claims until the leases expire, and a later re-check recovers it. No turn request ever waits for this work; until a rebuild succeeds, the prompt budget drops the oldest verbatim turns.
7. **Telemetry**, metadata only: span `docchat.maintenance.post_turn` (`docchat.session_id`, `docchat.turn_id`, `docchat.maintenance.extractions`, `docchat.maintenance.summary_ran`) and span `docchat.conversations.ensure_post_turn_work` (`docchat.session_id`, `docchat.summary.due`, `docchat.history.unsummarised_tokens`, `docchat.history.unsummarised_turns`, `docchat.maintenance.scheduled`).

## Alternatives considered

- **A queue and a separate worker.** Rejected: new infrastructure, identities and a network path to Cosmos DB for work that the leases already make safe and the turn-start re-check already recovers.
- **Background tasks attached to the streaming response.** Rejected: they would tie the work to the connection, whose end says nothing about the turn's outcome; the turn task's finaliser knows that the turn is `completed`.
- **Running the work inline before `done`.** Rejected: the work belongs after the answer, and a summary call would hold the stream open for seconds.
- **`asyncio.create_task` without a registry.** Rejected: the event loop holds only weak references, so a task could vanish mid-run, and the shutdown drain couldn't find it.
- **A Cosmos DB change feed processor on the API.** Rejected: it needs a lease container and a long-running processor in every instance, for work that a turn-start check finds directly.
- **Durable Functions for the work.** Rejected: the API holds no Durable client, and the ingestion app owns the only task hub (ADR 0003).

## Consequences

- No new resource or identity; every piece of state lives in Cosmos DB, so any instance can run, finish or recover the work.
- The work shares API workers with requests; it is I/O-bound and awaited, so it costs little event-loop time.
- A cancellation, restart or crash delays the work by at most the lease (60 s or 120 s) plus the time until the user's next turn; an extraction older than 24 hours is marked `expired` by the next re-check instead of running.
- The prefix query on `[oid]` spans the user's sessions, so it runs only off the chat path; an integration test proves that prefix queries happen only in `listSessions` and the post-turn work.
- Obligations (84-testing.md): an integration test that two post-turn tasks racing on one due session make exactly one `summary` call and one `version` increment; an integration test that a task killed after its claims leaves work that the next turn, in another session for extraction and the same session for the summary, completes once the leases expire; an integration test that SIGTERM during a stream lets the drain cancel a registered background task and the worker exit before 110 s; the spans of item 7 in 85-observability.md.

## References

- Python `asyncio.create_task` (the event loop keeps only weak references to tasks): https://docs.python.org/3/library/asyncio-task.html#asyncio.create_task (checked 2026-10-21)
- Azure Cosmos DB optimistic concurrency: https://learn.microsoft.com/en-us/azure/cosmos-db/database-transactions-optimistic-concurrency (checked 2026-10-14)
- Azure Cosmos DB partial document update (conditional patch): https://learn.microsoft.com/en-us/azure/cosmos-db/partial-document-update (checked 2026-10-14)
- Azure Cosmos DB hierarchical partition keys (prefix queries): https://learn.microsoft.com/en-us/azure/cosmos-db/hierarchical-partition-keys (checked 2026-10-14)
