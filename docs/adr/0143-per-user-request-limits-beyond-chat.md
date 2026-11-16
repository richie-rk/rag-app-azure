# ADR 0143: Per-user request limits beyond chat

- **Status:** Accepted
- **Date:** 2026-10-21
- **Decided by:** Architect
- **Specified in:** 11-api.md, 82-security.md, 81-config.md, 80-data-model.md, 85-observability.md, 84-testing.md
- **Related ADRs:** 0002, 0007, 0012, 0020, 0028, 0030, 0058, 0138, 0148

## Context

Chat has its own limits: a per-user sliding window on chat requests, a cap on concurrent streams and day and month token quotas, all checked before any model call, with counters in a store every API instance shares and `429` answers that carry `retryAfterSeconds` and `Retry-After` (ADR 0058). Every other operation is a cheap read or a small write, but each one still spends work in a shared store: a read runs SQL on the API's bounded SQL thread pool and Cosmos DB request units, a feedback or memory write is a Cosmos DB patch or replace, a memory write adds a SQL audit insert, and admin writes change SQL rows inside transactions. One signed-in account running a script could push the `conversations` container to its autoscale ceiling or saturate SQL and throttle chat for every user, and a loop of unbounded writes could fill the database until provisioning, project creation and ingestion fail for everyone. Every route except `GET /healthz` sits behind token validation, so each request already carries the caller's object ID.

Facts that bound the design:

- Cosmos DB patch supports an atomic `incr`, and a write conditioned on `_etag` fails with `412` when another write came first; items with a `ttl` expire on their own.
- The API runs one Gunicorn worker process per vCPU, each with its own event loop (2 per P1v3 instance, ADR 0007), so an in-process structure is per worker and needs no lock.
- The web client issues about 10 requests at once when a page loads and polls a screen at most every 5,000 ms.

## Decision

1. **Two limits, in the request pipeline's order.** After token validation (step 2) and the `User` or `Admin` role check (step 3), the burst limit runs at step 3a; after the `Admin` check of `/v1/admin/*` (step 4), the write limit runs at step 4a; both run before first-sign-in provisioning, the body read, validation and every lookup. `createChatTurn` skips both, because its own limits apply (ADR 0058); `GET /healthz` is outside `/v1` and passes neither.
2. **Burst limit.** Every `/v1` operation except `createChatTurn` passes an in-process token bucket keyed by the caller's `oid`, one map per Gunicorn worker process: capacity 40 tokens, refilled continuously at 10 tokens per second on the monotonic clock, one token per request; the map holds at most 50,000 users and evicts the least recently used. An empty bucket answers `429 too_many_requests` with `retryAfterSeconds` 1 and `Retry-After: 1`, before provisioning, ops config or any store is touched. The check runs on the event loop in well under 1 ms and takes no lock. The three values are code constants, not settings.
3. **Write limit: scope.** Every `POST`, `PUT`, `PATCH` and `DELETE` operation under `/v1` except `createChatTurn` and the four upload operations (`adminPreflightUploads`, `adminReserveUpload`, `adminUploadContent`, `adminSubmitUploads`, which the upload settings and `429 upload_busy` bound). In this version that is 17 operations: `putTurnFeedback`, `updateMemorySettings`, `updateMemoryFact`, `deleteMemoryFact`, `clearMemory`, `adminCreateProject`, `adminUpdateProject`, `adminAddProjectMember`, `adminRemoveProjectMember`, `adminMapProjectIndex`, `adminUnmapProjectIndex`, `adminCreateIndex`, `adminUpdateIndex`, `adminDeleteIndex`, `adminStartReingest`, `adminDeleteDocument` and `adminReingestDocument`. The route option `route_options(write_limited=…)` defaults to `True` for those four methods and only the five exempt routes declare `False`, so a new write operation is limited unless its owner opts out.
4. **Write limit: the window.** A two-bucket sliding minute in the caller's `usage` partition (`str(principal.oid)`), on `WriteRateWindow` documents: `id` `wrate:{epochMinute}` (whole minutes since 1970-01-01T00:00:00Z), `userId`, `type` `write_rate_window`, `count` (at least 1), `ttl` 180 seconds. At time `t` (UTC epoch seconds), `m = floor(t / 60)` and `f = (t − 60m) / 60`; with `c` the count of `wrate:{m}` and `p` that of `wrate:{m−1}` (0 when absent), the request is allowed when `(c + 1) + p × (1 − f) ≤ L`, where `L` is the ops-config key `api.write_requests_per_minute` (integer, default 120, range 10 to 1,200, area model `ApiOps`). The API point-reads both documents concurrently (timeout 2 seconds each) and, only when allowed, writes `wrate:{m}`: a patch `incr /count 1` conditioned on the ETag it read, or a create with `count` 1 when the document didn't exist (timeout 3 seconds). A `412` or `409` restarts the check, at most 5 attempts, after which the request gets `429 write_rate_limited` with `retryAfterSeconds` 1. A refused request isn't counted; an allowed one counts even when a later step refuses it (`404`, `409`, `412`, `422`).
5. **Write limit: the answers.** Over the limit: `429 write_rate_limited`, with `retryAfterSeconds` the smallest whole number `s` from 1 to 120 for which the window estimate at `t + s`, with no further requests, allows one more request (the same rule as chat's rate limit), and the same value in `Retry-After`. Cosmos DB unreachable, timed out or throttled after the SDK's retries (connection error, timeout, `429`, `5xx`): `503 dependency_unavailable` with `retryAfterSeconds` 5. The check fails closed.
6. **Telemetry and alert.** Both limits count refusals on `docchat.ratelimit.rejections` with `reason` `api` (burst) or `write`. Alert A43 `request-limit-rejections` (severity 3, 15-minute window evaluated every 5 minutes) fires when refusals with those two reasons exceed 200, the sign of a flooding account or a misbehaving client.
7. **No switch.** `create_app` receives both limiters (`BurstLimiter`, `WriteRateLimiter`) as ports; tests pass permissive ones except tests whose subject is a limiter's answer on a route (`real_limiters=True`). No setting disables a limiter in a deployed app.

## Alternatives considered

- **Chat limits only.** Rejected: every other route would be unbounded, and one account could throttle the shared stores for everyone.
- **One Cosmos DB window for every request, reads included.** Rejected: each cheap read would cost two point reads and a write in Cosmos DB, more than many reads cost themselves; the in-process bucket stops a flood before any store at no store cost, and only writes, which change shared state, pay for an exact shared count.
- **A rate-limiting gateway in front of the API.** Rejected: the product has no API gateway (ADR 0002), and a per-user key needs the validated `oid`, which only the app holds after token validation.
- **Per-IP limits on the platform.** Rejected: users behind one network address would share a limit, and a script spread over many addresses would escape it; the user's object ID is the identity the limits protect against.
- **Counting each calendar minute on its own.** Rejected: it allows twice the limit across a minute boundary; the two-bucket estimate smooths that at the same cost, and chat's rate limit uses the same arithmetic.
- **Failing open when Cosmos DB is unavailable.** Rejected: an unenforced limit is the flood it exists to stop, and the limited writes would themselves hit the struggling stores.
- **Burst values as settings.** Rejected: they protect the stores in every environment, and normal use stays far below them; a constant can't be loosened by mistake.

## Consequences

- One account can sustain about 10 requests a second per worker process after a burst of 40, and at most `api.write_requests_per_minute` limited writes a minute across all instances; a person clicking stays far below both.
- The burst ceiling grows with worker processes and instances, because buckets aren't shared; a restart, eviction or request served elsewhere only loosens it, which is acceptable for a flood guard that holds no access decision.
- Each limited write costs two point reads and one patch or create of a document under 1 KB in the caller's `usage` partition.
- During a Cosmos DB outage the 17 limited writes answer `503`, while reads keep working.
- A new write operation is limited by default; opting out needs `write_limited=False` and a change to the pinned exempt set in the same pull request.
- Load scenarios spread their cheap calls over many load users, so none approaches the burst limit.
- Tests: unit: 100 requests in 1 second from one `oid` (test clock) get at least 50 `429 too_many_requests`, each with `retryAfterSeconds` 1 and `Retry-After: 1`, and the refused ones touch no store; another `oid` is unaffected; `createChatTurn` is never refused by the bucket; after 1 second idle a refused `oid` gets 10 more; a 50,000-user map evicts the least recently used. Unit: every `POST`, `PUT`, `PATCH` and `DELETE` route of the OpenAPI document is `write_limited` except exactly the five exempt ones; request 120 is allowed and request 121 refused inside one minute; `retryAfterSeconds` follows the rule of item 5; a failing Cosmos DB fake gives `503 dependency_unavailable`; five ETag conflicts in a row give `429 write_rate_limited` with `retryAfterSeconds` 1. Integration (Cosmos DB emulator): starting at second 0 of a UTC minute and advancing 0.4 seconds per call, 130 `putTurnFeedback` calls give exactly 120 successes and 10 `429 write_rate_limited` with `retryAfterSeconds` from 1 to 60 and the same `Retry-After`; another user is unaffected; `wrate:{m}` holds `count` 120 and `ttl` 180.

## References

- Partial document update (patch `incr`, conditional patch): https://learn.microsoft.com/en-us/azure/cosmos-db/partial-document-update (checked 2026-10-14)
- Optimistic concurrency with `_etag`: https://learn.microsoft.com/en-us/azure/cosmos-db/database-transactions-optimistic-concurrency (checked 2026-10-14)
- Time to live: https://learn.microsoft.com/en-us/azure/cosmos-db/time-to-live (checked 2026-10-14)
- RFC 9110, HTTP Semantics (`Retry-After`): https://www.rfc-editor.org/rfc/rfc9110 (checked 2026-10-21)
- RFC 9457, Problem Details for HTTP APIs: https://www.rfc-editor.org/rfc/rfc9457 (checked 2026-10-21)
- Python `time.monotonic`: https://docs.python.org/3.13/library/time.html (checked 2026-10-21)
