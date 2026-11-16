# ADR 0050: Backoff through durable timers

- **Status:** Accepted
- **Date:** 2026-10-21
- **Decided by:** Architect
- **Specified in:** 14-ingestion.md, 81-config.md, 84-testing.md, 85-observability.md
- **Related ADRs:** 0028, 0034, 0035, 0036, 0046, 0048, 0051, 0053, 0113, 0144

## Context

Ingestion runs one activity per unit of work, with a checkpoint after each activity and retries per activity, so a failure resumes at the failed step and one failed document never fails its run. A document's sub-orchestration runs up to nine activities (Validate, Extract, Enrich, Chunk, Embed, Index, Render, Flip, Clean up), several of which call throttled services: table summaries and embeddings on Azure OpenAI, layout analysis on Document Intelligence, uploads on Azure AI Search. Retries back off exponentially, and calls to Azure OpenAI and Document Intelligence honour `Retry-After` on `429`. A throttle can last minutes, and a wait must not hold a worker process or one of the instance's activity slots, which ADR 0053 sizes at two per parse slot; paid work such as table summaries must not be repeated by a retry (ADR 0051).

Vendor facts (checked 2026-10-14, and two Microsoft Learn pages checked 2026-10-21):

- `df.RetryOptions(first_retry_interval_in_milliseconds, max_number_of_attempts)` takes both values as required and emits only them; the extension would accept a backoff coefficient, a maximum interval and a retry timeout, but the Python SDK never sends them, and the coefficient defaults to 1. Microsoft's error-handling page states that Python doesn't support custom retry handlers and that retries are built from loops, exception handling and durable timers.
- A Python orchestrator creates a timer with `context.create_timer(fire_at)`, takes time only from `context.current_utc_datetime`, and must be deterministic, because every wake replays it from the start; one timer can wait at most 6 days.
- `openai` 3.30.0 and later retry 408, 409, 429 and 5xx (2 retries by default), read `retry-after-ms` then `retry-after`, and don't retry when the server asks for more than 120 seconds. Azure OpenAI's `429` responses carry `retry-after-ms`; Document Intelligence's analyze responses carry `retry-after`.
- azure-core's defaults: `retry_total` 10, `retry_connect`, `retry_read` and `retry_status` 3 each, `retry_backoff_factor` 0.8, `retry_backoff_max` 120 seconds. The storage SDKs' `ExponentialRetry` defaults to `initial_backoff=15`, `increment_base=3`, `retry_total=3`, `random_jitter_range=3`, waiting `initial_backoff` and then `initial_backoff + increment_base^retry_count` seconds (about 15, 18 and 24 seconds).

## Decision

1. **Typed results.** No activity raises for an expected failure. Every activity returns its output model with the `ActivityResult` fields `ok` (bool), `errorCode` (string or null), `retryable` (bool) and `retryAfterSeconds` (integer or null); `retryable` comes from the code's row of the ingestion item-code catalogue (14-ingestion.md). An unexpected exception is returned as retryable `internal_error` by the failure boundary, and an activity that passes its deadline returns retryable `activity_timeout` (ADR 0144).
2. **One retry wrapper.** `document_item_orchestrator` and `index_item_orchestrator` call every activity through the generator helper `call_with_retry(context, name, input, retry)`. It makes attempt 1; returns at once on `ok`; returns the failure at once when `retryable` is false; otherwise, while attempts remain, waits on `context.create_timer(context.current_utc_datetime + timedelta(seconds=wait))` and makes the next attempt; after the last attempt it returns the failure. A failed activity task (the host reports the invocation itself failed) raises at the `yield`; the wrapper catches it and treats it as the retryable code `internal_error`. The sub-orchestration then records the outcome: `record_item_outcome(failed, code, stage)`, then `clean_up_item` in discard mode.
3. **The schedule.** Up to `ingestion.retry_max_attempts` attempts (default 7, range 1 to 10). The wait before attempt `n + 1` is `min(max(min(retry_first_delay_seconds × 2^(n−1), retry_max_delay_seconds), retryAfterSeconds), 540)` seconds, with a null `retryAfterSeconds` counted as 0, `ingestion.retry_first_delay_seconds` (default 15, range 1 to 300) and `ingestion.retry_max_delay_seconds` (default 480, range 10 to 540): waits of 15, 30, 60, 120, 240 and 480 seconds, 945 seconds (about 16 minutes) in all. No wait exceeds 540 seconds: a `Retry-After` above 540 seconds is waited as 540 seconds, and the next attempt calls the service and, if it is still throttled, honours its new `Retry-After` the same way. The dispatcher puts the three values into each item's input (`ItemInput.retry {maxAttempts, firstDelaySeconds, maxDelaySeconds}`) from its settings snapshot when it claims the item, so an item runs with one consistent set. Two calls use their own attempt counts: `clean_up_item` after a flip makes 3 attempts and then records the warning `cleanup_incomplete` (the sweep finishes the job later), and in discard mode 2.
4. **`Retry-After` reaches the orchestrator.** Inside one attempt the SDK clients retry for seconds. When they give up on a throttled or unavailable service, the activity returns that service's retryable code from the item-code catalogue, and `retryAfterSeconds` is set from the last response's `retry-after-ms` (rounded up to whole seconds) or `Retry-After` (seconds) when that response carried one, and is null otherwise; the long wait then happens on a durable timer, never inside the activity.
5. **In-attempt budgets of the clients** (options set in `docchat_core.clients`, ADR 0113):
   - `AsyncOpenAI`, ingestion profile: `max_retries=3`, `timeout=httpx2.Timeout(60.0, connect=5.0)`; each retry honours `retry-after-ms` or `retry-after` up to 120 seconds.
   - Blob and Queue clients of both apps: `retry_policy=ExponentialRetry(initial_backoff=1, increment_base=2, retry_total=3, random_jitter_range=1)`, waits of about 1, 3 and 5 seconds, so an API request on the upload path (`stage_block`, the queue send of a run-creating operation) answers within seconds rather than about a minute.
   - Search and Document Intelligence clients: azure-core's default `RetryPolicy` (at most 3 retries each for status, connection and read failures and 10 in all, backoff factor 0.8 seconds capped at 120 seconds, honouring `Retry-After`).
6. **No built-in retry options.** `call_activity_with_retry`, `call_sub_orchestrator_with_retry` and `df.RetryOptions` are used nowhere: they give constant intervals with no cap and no `Retry-After`, and they retry only raised exceptions, whose text would enter Durable history (ADR 0144).
7. **The dispatcher's own activities.** `load_dispatch_settings`, `sweep_ingestion_state` and `claim_ingestion_items` aren't wrapped: a failed result makes the dispatcher wait 60 seconds and try again at its next wake (ADRs 0035 and 0036), so a SQL outage pauses work without a retry loop.
8. **Determinism and telemetry.** The wrapper decides only from recorded activity results and `context.current_utc_datetime`, so a replay makes the same waits. Item activities count `docchat.ingestion.throttled` (dimensions `service` = `openai`, `docintel` or `search`, and `deployment`) when a service throttles, and the activity span `docchat.ingestion.activity` carries `docchat.attempts`.

## Alternatives considered

- **`call_activity_with_retry` with `df.RetryOptions`.** Rejected: the Python SDK sends only a first interval and an attempt count, so every retry waits the same time, nothing caps the total and `Retry-After` is ignored; it also retries only raised exceptions.
- **Sleeping inside the activity on `Retry-After`.** Rejected: the wait would hold a worker and one of the instance's activity slots, a long wait would run into the activity's deadline, and a host restart would lose it.
- **The SDK clients' retries alone.** Rejected: their budgets are kept to seconds (two minutes for `openai`) so that one attempt ends quickly; a throttle that lasts minutes needs a wait that holds nothing.
- **Unlimited retries.** Rejected: a document that can never succeed would keep its parallelism slot for ever; after 7 attempts the item fails with its code and the run goes on.
- **Random jitter on the orchestrator's waits.** Rejected: orchestrator code must be deterministic on replay, and with at most 16 items running at once (default 1) synchronised retries stay few; the storage clients keep 1 second of jitter inside an attempt.

## Consequences

- A throttled service delays an item by minutes without holding a worker or an activity slot; the item keeps its parallelism slot while it waits, and other items' activities keep running.
- An item fails at the latest after 7 attempts: 945 seconds of waits with the defaults, and at most 6 × 540 = 3,240 seconds when every wait honours a capped `Retry-After`. A deployment during a running item is absorbed by the same schedule (ADR 0046).
- Each attempt extends the item's lease to its start plus the activity's deadline plus 10 minutes (ADR 0034). Every wait is at most 540 seconds, and `ingestion.retry_max_delay_seconds` accepts nothing above 540, so every wait ends inside the lease's 10-minute margin and a throttle never costs a re-claim. A service that asks for a longer wait is called after 540 seconds and answers with a new `Retry-After`, which costs one attempt.
- Obligations (84-testing.md): a unit test of the wrapper (waits of 15, 30, 60, 120, 240 and 480 seconds; a `Retry-After` of 500 seconds honoured and one of 2,000 waited as 540; no wait exceeds 540 seconds; a non-retryable code stops at once); a unit test of the client options of item 5; the dispatcher replay harness covering the wrapper; the three retry keys seeded in `ops_config` with their ranges (81-config.md).

## References

- Durable Functions error handling (retry policies; no custom retry handlers in Python; loops with durable timers): https://learn.microsoft.com/en-us/azure/azure-functions/durable/durable-functions-error-handling (checked 2026-10-14)
- `RetryOptions` at v1.7.0: https://raw.githubusercontent.com/Azure/azure-functions-durable-python/v1.7.0/azure/durable_functions/models/RetryOptions.py (checked 2026-10-14)
- Durable timers: https://learn.microsoft.com/en-us/azure/azure-functions/durable/durable-functions-timers (checked 2026-10-14)
- Orchestration replay and determinism: https://learn.microsoft.com/en-us/azure/durable-task/common/durable-task-orchestrations (checked 2026-10-14)
- `openai` retry behaviour: https://pypi.org/project/openai/3.30.0/ (checked 2026-10-14)
- Azure OpenAI quotas and `429` responses: https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/quota (checked 2026-10-14)
- Document Intelligence service limits: https://learn.microsoft.com/en-us/azure/ai-services/document-intelligence/service-limits?view=doc-intel-4.0.0 (checked 2026-10-14)
- azure-core retry and transport defaults: https://learn.microsoft.com/en-us/python/api/overview/azure/core-readme?view=azure-python (checked 2026-10-21)
- Storage `ExponentialRetry`: https://learn.microsoft.com/en-us/python/api/azure-storage-blob/azure.storage.blob.exponentialretry?view=azure-python (checked 2026-10-21)
