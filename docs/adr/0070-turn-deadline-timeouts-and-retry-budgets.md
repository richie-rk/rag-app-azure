# ADR 0070: Turn deadline, timeouts and retry budgets

- **Status:** Accepted
- **Date:** 2026-10-18
- **Decided by:** Architect
- **Specified in:** 11-api.md, 12-chat-pipeline.md, 13-conversations-and-memory.md, 81-config.md, 83-infrastructure.md, 84-testing.md, 85-observability.md, 86-operations.md
- **Related ADRs:** 0002, 0006, 0007, 0008, 0017, 0026, 0056, 0057, 0058, 0062, 0068, 0069, 0073, 0074, 0075, 0148

## Context

A chat turn is one HTTP response: an NDJSON stream that passes App Service's front end and the platform-authentication (Easy Auth) container in front of the API. While it runs, the turn holds one of the user's stream leases, the session's active-turn guard, a share of model quota and a task on a worker's event loop. Without explicit bounds, one slow dependency would hold all of these for as long as the slowest client default allows. The product needs every turn to end in bounded time with exactly one terminal event, every outbound call to carry explicit timeouts, and a stopped instance to fail only the streams it held.

Two values are product rules: a `heartbeat` line after 15 s without output, and a 5-second timeout on the follow-ups call.

Platform and library facts (checked 2026-10-14 unless dated otherwise):

- App Service's app-settings reference describes a general idle request timeout that disconnects clients after 230 s; the troubleshooting page puts the request timeout at about 240 s on Linux (230 s on Windows), from Azure Load Balancer's 4-minute idle timeout.
- On Linux, platform authentication runs as a separate ambassador container that every request passes through. No page documents whether it buffers streamed bodies, or whether the request timeout counts from the last byte or from the start; only deployed streaming tests settle it.
- App Service waits at most 120 s for a container to stop after its stop signal (`WEBSITES_CONTAINER_STOP_TIME_LIMIT`, default 5 s).
- The `openai` package's client (3.30.0 source) retries connection errors, `408`, `409`, `429` and `5xx` twice by default, waits up to 120 s on a `Retry-After`, and allows 600 s per request with a 5-second connect timeout; azure-core's default for the Search and Document Intelligence clients is 300 s to connect and 300 s to read; the storage SDK's default retry waits about 15, 18 and 24 s.
- azure-cosmos (4.17.1) accepts a `timeout` keyword on every operation, "an absolute timeout in seconds within which the operation must be completed", and raises `CosmosClientTimeoutError` when it is exceeded (checked 2026-10-18); this design passes it on every call and treats it as covering the SDK's own retries.

## Decision

**1. Turn cap.** `chat.max_turn_seconds` (ops config; integer; default 180; range 60 to 220) counts from `received_at`, the moment the route received the request. The deadline it sets wraps steps T2 to T13 of the turn task:

- passing before finalisation (T11): every in-flight call is cancelled, the turn is saved `failed` with the text released so far, and the stream ends with `error` `turn_timeout` (retryable);
- passing after T11: it cancels only the follow-ups call, and the stream ends with `done`.

The pre-stream checks (P1 to P9) run before the turn task exists and are bounded by their own operation timeouts (items 3 and 4). A `{"type":"heartbeat"}` line after every 15 s without output (a constant) keeps the stream from going idle inside the cap. 180 s keeps a 50-second margin under the 230-second limit, which may count from the start of the request rather than the last byte; the 220-second maximum keeps 10 s.

**2. Deadline and lease offset.** `deadlineAt` = `received_at` + `chat.max_turn_seconds` + 30 s (210 s by default), stored in the session's `activeTurn` and on the turn. A stream lease's `expiresAt` and item `ttl` = its acquisition time + `chat.max_turn_seconds` + 30 s. The 30 s cover the 15-second shielded finaliser that follows the cap, with 15 s to spare, and the lease is acquired after receipt, so it never expires before its turn's deadline. A turn still `streaming` after `deadlineAt` belongs to a process that died: the next turn or a duplicate marks it `failed` with `turn_abandoned` (ADR 0056), and its lease expires by TTL. `409 turn_in_progress` carries `retryAfterSeconds` = whole seconds until `deadlineAt`, rounded up, at least 1. No write renews a deadline or a lease while a turn runs.

**3. Client timeouts.** Every value is explicit; none is an SDK default.

| Dependency (API) | Connect | Read, or whole call | Set by |
|---|---|---|---|
| Azure OpenAI, every call | 10 s | 60 s of silence between streamed chunks or before a non-streamed response; write 10 s; pool 10 s | `DOCCHAT_OPENAI_CONNECT_TIMEOUT_SECONDS` (10, 1 to 30), `DOCCHAT_OPENAI_READ_TIMEOUT_SECONDS` (60, 10 to 120), in the `httpx2.Timeout` of ADR 0068 |
| Rewrite, search passage, follow-ups | inside the call budget | the whole call, connection included: 8 s, 8 s and 5 s | `chat.rewrite_timeout_seconds` (8, 2 to 20), `retrieval.broaden_timeout_seconds` (8, 2 to 20), `followups.timeout_seconds` (5, 2 to 10) |
| Query and passage embeddings | inside the call budget | 10 s per attempt | constant |
| Summary and memory (after the response) | inside the call budget | 30 s and 20 s per attempt | `summary.timeout_seconds` (30, 5 to 120), `memory.timeout_seconds` (20, 5 to 60) |
| AI Search | 10 s | 10 s per request | `connection_timeout=10`; `read_timeout` = `DOCCHAT_SEARCH_TIMEOUT_SECONDS` (10, 2 to 30) |
| Prompt Shields (`httpx`) | 5 s | 5 s per call | `DOCCHAT_CONTENTSAFETY_TIMEOUT_SECONDS` (5, 1 to 15) |
| Cosmos DB `usage` | 5 s | per operation, retries included: 2 s for point reads; 3 s for create, patch, replace and delete | constants |
| Cosmos DB `conversations` | 5 s | point read 2 s; query page 5 s; batch, patch, replace and create 5 s | the conversation store's constants |
| Blob Storage (citation files) | 10 s | 60 s per network read | `connection_timeout=10`, `read_timeout=60` |
| Azure SQL | 30 s (login) | 30 s per statement; 10 s pool wait | `DOCCHAT_SQL_CONNECT_TIMEOUT_SECONDS`, `DOCCHAT_SQL_COMMAND_TIMEOUT_SECONDS`, `DOCCHAT_SQL_POOL_TIMEOUT_SECONDS` (ADR 0026) |
| Entra signing keys (`httpx`) | 4 s | 8 s | the signing-key cache (ADR 0017) |

**4. Retry budgets.** The API's `AsyncOpenAI` client has `max_retries=0`, so only these rules retry a model call.

| Call | Attempts and rule | When attempts run out |
|---|---|---|
| Answer | 2 attempts, only before the first streamed chunk arrives, on connection errors, `408`, `429` whose `retry-after-ms` or `retry-after` delay is at most 10 s, and `500`, `502`, `503`, `504`, waiting the stated delay or 1 s; never after the first chunk | a `429` → `error` `model_busy` with `retryAfterSeconds` from the header (1 to 60); a connection error, timeout or `5xx` → `model_unavailable` |
| Rewrite, search passage, follow-ups | 1 attempt | the call's fallback: the question as written, the first search results, no follow-ups |
| Query and passage embeddings | 2 attempts, on connection errors, `408`, `429` with a retry delay of at most 5 s, and `5xx` | `retrieval_unavailable`; for the passage, the first results are kept |
| Summary, memory | at most 2 attempts per run, on connection errors, `408`, `429` whose delay is at most 10 s, and `500`, `502`, `503`, `504`, waiting the stated delay or 2 s; never after `400`; worst case 70 s inside the 120-second summary lease and 50 s inside the 60-second memory lease | the job's failure rules (ADR 0074, ADR 0075) |
| AI Search | the client's retry policy: 2 retries, exponential backoff from 0.5 s to 4 s | `retrieval_unavailable` |
| Prompt Shields | 1 retry per call, after 0.5 s or after a `Retry-After` of at most 2 s, on a timeout, connection error, `429` or `5xx` | `source_screening_unavailable` (retryable), before any answer call |
| `usage`, before the response starts (rate windows, quota reads, lease acquisition, download window and lease) | a timed-out operation is retried once | `503 dependency_unavailable` with `retryAfterSeconds` 5, failing closed; the background quota gate raises `UsageStoreUnavailable` |
| `usage` counter writes, after `200` | up to 3 attempts, 0.2 s then 0.4 s apart, on `408`, `429`, `449`, `503` and a client timeout | log `usage_record_failed`, increment `docchat.usage.record_failures`; the turn goes on |
| `usage` lease release | 1 attempt | a timed-out release is left to expire by TTL |
| `conversations` | a `429` is retried by the SDK, at most 3 attempts and 5 s in total | `503 dependency_unavailable` before the stream; `conversation_unavailable` or `turn_save_failed` after it |
| Blob Storage (citation files) | `ExponentialRetry(initial_backoff=1, increment_base=2, retry_total=3, random_jitter_range=1)`: waits of about 1, 3 and 5 s | the citation errors of ADR 0069 |

**5. Finaliser and drain.** Every path of the turn task ends in one finaliser under `asyncio.shield` with a 15-second budget: `finalize_turn` when T11 wasn't attempted, the usage writes awaited, the lease released if still held and, for a `completed` turn, the after-response work scheduled. On the stop signal the drain of ADR 0007 runs: 90 s later, a turn already finalised `completed` drops its follow-ups and writes `done`; any other running turn writes `error` `service_restarting` (retryable, `retryAfterSeconds` 5) when its stream is open and is stored `failed` with that code; running citation downloads are aborted. Every turn is saved by 105 s, inside the 110-second graceful timeout (`DOCCHAT_API_GRACEFUL_TIMEOUT_SECONDS`) and the 120-second stop limit (`WEBSITES_CONTAINER_STOP_TIME_LIMIT` = 120).

**6. Checked in CI.** `scripts/ci/check_outbound_clients.py`, an AST check in the `lint` job, fails when an `httpx.AsyncClient(` call has no `timeout=` or passes `follow_redirects=True`, when a `urlopen(` call has no `timeout=`, when an Azure SDK client constructor inside `docchat_core.clients` lacks `connection_timeout=` or `read_timeout=` (Cosmos DB: `connection_timeout=`), or when such a client or `openai.AsyncOpenAI` is created outside `docchat_core.clients`. Every Cosmos DB call passes its per-operation `timeout`.

## Alternatives considered

- **A cap at the platform's limit (230 to 240 s).** Rejected: no margin would remain for the shielded finaliser, and Microsoft doesn't document whether the limit counts from the start of the request; a stream cut by the platform ends with no terminal event.
- **No cap, only per-call timeouts.** Rejected: read timeouts bound silence, not length, so an answer that keeps streaming slowly would run without limit, and the deadline and lease offsets need a known maximum turn length.
- **The SDK defaults.** Rejected: the `openai` client's 2 retries with waits of up to 120 s and a 600-second request timeout, and azure-core's 300-second connect and read timeouts, can each outlast a whole turn and the front end's limit.
- **Retrying the answer after the first chunk.** Rejected: the user has already seen text, so a retry would repeat or contradict it and pay for the prompt twice.
- **Renewing the deadline and the lease while a turn runs.** Rejected: renewal writes for every open stream add `usage` and `conversations` traffic; a deadline with a known maximum bounds how long a crashed instance can block a session without them.
- **Failing open when the `usage` store is slow.** Rejected: rate limits and quotas would stop applying exactly while the store is degraded; the pre-stream checks fail closed with a retryable `503`.

## Consequences

- Every turn ends within its cap plus the 15-second finaliser, with exactly one terminal event; every lease and active-turn guard frees itself within the cap plus 30 s (210 s by default, 250 s at the 220-second maximum), even when an instance dies.
- The cap, not the sum of per-call budgets, bounds a turn: when several dependencies are slow at once, the turn ends with `turn_timeout` before every per-call budget is spent.
- Settings (81-config.md): `chat.max_turn_seconds` and the three per-call budgets in ops config, range-checked by their Pydantic models on every load (ADR 0008); `DOCCHAT_OPENAI_CONNECT_TIMEOUT_SECONDS`, `DOCCHAT_OPENAI_READ_TIMEOUT_SECONDS`, `DOCCHAT_SEARCH_TIMEOUT_SECONDS` and `DOCCHAT_CONTENTSAFETY_TIMEOUT_SECONDS` as app settings, range-checked at start-up.
- Tests (84-testing.md): integration, a stub that streams for 200 s with `chat.max_turn_seconds = 180` ends with `turn_timeout` at 180 s ± 2 s; a 40-second stub pause before the first chunk yields at least 2 heartbeats 15 s ± 1 s apart; a turn whose `activeTurn` deadline passed is taken over and the stale turn becomes `failed` with `turn_abandoned`; the drain test of ADR 0007; unit, a rate-window point read that never returns gives `503 dependency_unavailable` with `retryAfterSeconds` 5 after 2 × 2 s, a lease create that never returns gives `503` after 2 × 3 s, a counter patch after `200` that never returns logs `usage_record_failed` while the turn still ends with `done`, and `check_token_quota` raises `UsageStoreUnavailable`; deployed, a 200-second stub stream with heartbeats passes platform authentication and the App Service front end uncut, and a browser disconnect reaches the API within 2 s; the stream-buffering spike of stages 2 and 3, in which a chat stream's `meta` line must arrive within 3 s and a failure stops the build for the product owner's decision with the measurements; load, stopping one instance mid-test fails only its streams and every lease expires within 210 s; CI, the outbound-client check of item 6.
- Monitoring (85-observability.md): `docchat.chat.turns` by `outcome` and `error_code` (`turn_timeout`, `model_busy`, `model_unavailable`), `docchat.chat.turn.duration`, `docchat.openai.throttled` and `docchat.chat.drained_turns`; alerts A07 `chat-failed-turns` (more than 5% of at least 20 turns in 15 minutes), A08 `chat-openai-throttling` (more than 5% of at least 20 calls in 10 minutes), A10 `usage-recording-failures` (any in 15 minutes), A41 `openai-first-chunk-latency` and A42 `openai-call-latency`.
- Runbooks (86-operations.md): "Too many answers in progress" needs no manual step, because leases expire within the cap plus 30 s; the drain runbook of ADR 0007 covers `service_restarting`.
- Risk: a slow model on a long answer reaches `turn_timeout`, which stores the partial text as `failed`; the cap can be raised to 220 s with `docchat-ops config set` and no release.
- Risk: how platform authentication treats long streams is undocumented. The deployed 200-second stream test and the stream-buffering spike are the checks; a buffered stream stops the build for the product owner's decision. The cap is an operator setting from 60 to 220 s, so a lower value needs no release.

## References

- App Service request timeout (about 240 s on Linux): https://learn.microsoft.com/en-us/troubleshoot/azure/app-service/web-request-times-out-app-service (checked 2026-10-14)
- App Service app settings (230-second idle timeout; `WEBSITES_CONTAINER_STOP_TIME_LIMIT`, maximum 120 s): https://learn.microsoft.com/en-us/azure/app-service/reference-app-settings (checked 2026-10-14)
- App Service authentication (the ambassador container on Linux): https://learn.microsoft.com/en-us/azure/app-service/overview-authentication-authorization (checked 2026-10-14)
- `openai` client defaults: https://pypi.org/project/openai/3.30.0/ and https://pypi.org/project/openai/ (3.31.0) (checked 2026-10-14)
- Azure OpenAI rate limits (`retry-after-ms` on `429`): https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/quota (checked 2026-10-14)
- azure-cosmos per-operation `timeout` and default connection timeout: https://pypi.org/project/azure-cosmos/ (4.17.1, checked 2026-10-18)
