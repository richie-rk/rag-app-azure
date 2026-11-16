# ADR 0144: Failure boundary for the ingestion app

- **Status:** Accepted
- **Date:** 2026-10-21
- **Decided by:** Architect
- **Specified in:** 14-ingestion.md, 82-security.md, 83-infrastructure.md, 84-testing.md, 85-observability.md, CONTEXT.md
- **Related ADRs:** 0014, 0024, 0026, 0032, 0034, 0036, 0049, 0050, 0053, 0113, 0132, 0150

## Context

The ingestion app feeds files written by other people through large libraries: `unstructured`, pdfminer, Pillow, OpenCV, Tesseract, qpdf through `pikepdf`, lxml and the Azure SDKs. Their exception messages can quote what they read: a parser or JSON error quoting extracted text, a validation error printing input values, a service's error body. Wherever such an exception escapes, the platform records it outside the app's telemetry processors: the Functions host writes a failed function's exception (category `Function.<name>`, collected by `FunctionAppLogs` and the host's own Application Insights logging), and Durable Functions stores an activity's or orchestrator's failure details in task-hub history, kept for 30 days (ADR 0032). Logs and traces of this product carry metadata only, never document content or file names; errors never carry exception text, stack traces, SQL or upstream messages; every failure is logged once, at the boundary that handles it, and never swallowed. Credentials never travel in orchestration inputs, so the task hub holds IDs, codes and blob paths only.

A call that never answers is the other failure to contain. It would hold one of the instance's activity slots (two per parse slot, ADR 0053) until the host gives up, and the dispatcher's own short activities could queue behind it. Every document gets a time and a memory limit, and every piece of ingestion work needs its own deadline.

Vendor facts:

- On the Premium plan `functionTimeout` defaults to 30 minutes and has no maximum (checked 2026-10-14).
- azure-core's transport defaults are `connection_timeout` 300 seconds and `read_timeout` 300 seconds (checked 2026-10-21).
- `asyncio.timeout(delay)` cancels the task when the delay elapses and turns the resulting `CancelledError` into `TimeoutError`, which can be caught only outside the context manager; `Timeout.expired()` tells whether the deadline was exceeded; structured-concurrency tools misbehave when code swallows `CancelledError`, which subclasses `BaseException` (Python 3.13, checked 2026-10-21).
- With `traceInputsAndOutputs` false, Durable traces log only payload sizes; an unhandled exception moves an orchestration to `Failed` (checked 2026-10-14).

## Decision

1. **One guard.** `docchat_ingestion.boundary.guard(<function name>)`, an async context manager in `apps/ingestion/src/docchat_ingestion/boundary.py`, wraps the body of every activity and of each of the three trigger functions (`ingestion_queue_starter`, `ingestion_keepalive`, `ingestion_maintenance`). It catches every exception except `asyncio.CancelledError` and records it exactly once: `log_event` with the event `activity_failed` (activities) or `job_failed` (triggers) and the attributes `error.type` = the exception's qualified type name (`module.QualName`) and `docchat.error_code` = `internal_error`; and `record_failure(span, exc, "internal_error")`, which stores the code's catalogue message and the frames-only stack trace of ADR 0014. `str(exc)` and the exception's arguments are never recorded anywhere.
2. **What the guard returns.** An activity returns `ActivityResult(ok=False, errorCode="internal_error", retryable=True)`, retried within ADR 0050's attempts. `ingestion_keepalive` and `ingestion_maintenance` log and return, and run again at their next tick (5 minutes later; 02:40 UTC the next day). `ingestion_queue_starter` alone re-raises, and only for exceptions from its Durable client calls (`ensure_dispatcher`, `raise_event`): after logging it raises the first-party `DispatcherUnavailableError`, whose message is the constant `durable client call failed`, with `from None`, so the queue redelivers the message (it reappears after about 10 minutes and moves to `ingestion-requests-poison` after 5 dequeues) and the host's failure record holds no library text. A message that fails validation emits the event `ingestion_message_invalid` (`messageId`) and the starter returns normally, so the message is deleted.
3. **Expected failures never raise.** Activities return the codes of the ingestion item-code catalogue, whose safe messages are what the item stores (`outcome_message`); exceptions are only for the unexpected. Orchestrators are generator functions that do no I/O, read time only from `context.current_utc_datetime` and raise only first-party exceptions with constant messages, so no library text reaches Durable history from an orchestrator either. The dispatcher logs nothing itself; an invariant exception that escapes its loop fails the instance, and the keep-alive restarts it within 5 minutes (ADR 0036).
4. **No shared mutable state.** Activity code keeps no module-level mutable state. Shared async clients come from `docchat_core.clients` (ADR 0113), created once per worker process at its first invocation and closed at worker shutdown; per-document facts travel only in typed inputs and results.
5. **Activity deadlines.** Inside the guard, each activity body runs in `asyncio.timeout(<deadline>)`:

   | Activity | Deadline (seconds) |
   |---|---|
   | `load_dispatch_settings`, `claim_ingestion_items` | 60 |
   | `validate_item`, `flip_chunk_set`, `clean_up_item`, `record_item_outcome` | 120 |
   | `sweep_ingestion_state`, `create_search_index`, `delete_search_index` | 300 |
   | `chunk_document` | 660 |
   | `index_chunks` | 1,200 |
   | `enrich_document`, `embed_chunks`, `purge_document_artifacts`, `purge_index_blobs` | 1,560 |
   | `render_pages`, `rerender_pages` | 2,400 |
   | `extract_document` | 2,700 |

   When `TimeoutError` arrives and the context's `expired()` is true, the activity has already cancelled its work: every process of a running parsing child's UID is ended (ADR 0049 decision 14), its scratch directory is removed in a `finally` block, and an in-flight SQL unit of work is abandoned, its pool thread committing or rolling it back when the statement returns, within the ingestion app's 120-second command timeout (ADR 0026). The activity returns `ActivityResult(ok=False, errorCode="activity_timeout", retryable=True)`. Every activity's writes are idempotent on a re-run (the build key, the flip's served-row check, fenced and state-guarded updates), so a unit of work that commits after its activity gave up does no harm. An item activity's lease runs to its deadline plus 10 minutes (ADR 0034). `ingestion.activity_time_budget_seconds` (`ops_config`, default 1,200, range 60 to 1,200) ends each call of Enrich, Embed and the blob purges with `done: false` at least 300 seconds inside its 1,560-second deadline, and the orchestrator calls it again, at most 20 times.
6. **A finite host timeout.** `host.json` sets `functionTimeout` to `01:00:00`, above the longest deadline (2,700 seconds), so the host ends a function only if the app's own deadline has failed.
7. **Transport timeouts on every client**, passed by `docchat_core.clients` and never left at an SDK default: Blob and Queue clients of both apps `connection_timeout=10`, `read_timeout=60` seconds; the ingestion app's `SearchClient` and `SearchIndexClient` `connection_timeout=10`, `read_timeout=120`; `DocumentIntelligenceClient` `connection_timeout=10`, `read_timeout=300`; `AsyncOpenAI` (ingestion profile) `timeout=httpx2.Timeout(60.0, connect=5.0)` with `max_retries=3`. `scripts/ci/check_outbound_clients.py` fails the build when a client in `docchat_core.clients` is built without `connection_timeout=` and `read_timeout=` (ADR 0150).
8. **One record per failure, metadata only.** Besides the guard's `activity_failed` and `job_failed`, an expected failure appears as its code on the span `docchat.ingestion.activity` (`docchat.outcome`, `docchat.error_code`), and the only warnings are `sweep_duty_failed` (`duty`), `maintenance_failed` (`step`) and `scan_time_unusable`; nothing is logged at debug level in production. `host.json` keeps the host at `Warning` with Durable's `traceInputsAndOutputs` false, and activity inputs and outputs carry IDs, codes, counts and blob paths only (ADR 0014).

## Alternatives considered

- **Let exceptions reach the host and rely on the scrubbing filter.** Rejected: the host's failure record and Durable's stored failure details are written by the platform, outside the app's processors, and no pattern recognises document text.
- **Durable's built-in retry of raised exceptions.** Rejected: it retries at constant intervals and stores every exception's text in history (ADR 0050).
- **A `try` block at each call site.** Rejected: every new code path would need its own handler and one missed handler leaks; one guard per function covers every path, and the catalogue codes still come from the call sites that expect them.
- **`functionTimeout` as the only limit.** Rejected: one limit for every function would let a 120-second Validate hang as long as a 45-minute Extract, the activity slot would stay busy meanwhile, and the host records its timeout as a failed function.
- **SDK default timeouts.** Rejected: 300 seconds to connect and 300 per read let one stuck connection hold an activity slot for most of a deadline, and the dispatcher's 60-second activities could wait behind it.
- **Timer functions that let failures escape.** Rejected: the host would record library text, and the next tick retries anyway; logging once and returning keeps both functions on schedule.

## Consequences

- Every unexpected failure has exactly one log record, with a type name and a code; no library text reaches host logs, Durable history, spans or SQL, and the 30-day history purge (ADR 0032) only ever holds IDs, codes and paths.
- An unexpected exception or a passed deadline is retried by the same schedule as a throttle, and after the last attempt the item fails with `internal_error` or `activity_timeout`, whose messages tell the admin to process the document again.
- Harder: debugging works from type names, codes, frames and IDs. A failure that needs its message is reproduced from the stored inputs under just-in-time access (ADR 0024).
- Risk: `asyncio.timeout` fires only when the event loop runs. Work in a thread (the chunker runs in `asyncio.to_thread`) keeps its thread until it returns, though the activity slot is freed at the deadline; parsing runs in a child process that the parent's own clock kills (ADR 0049); an event-loop test under asyncio debug mode and the event-loop guard (ADR 0132) catch blocking calls on the loop.
- Obligations (84-testing.md): a blob download that never returns ends `extract_document` with `activity_timeout` at 2,700 seconds on the fake clock, removes its scratch directory and frees the activity slot; a chunker dependency raising `ValueError("cnry-chunk-<8 hex>")` returns `internal_error`, and no Durable-harness history entry, log record or span contains `cnry-`, also for exceptions in the keep-alive and the maintenance timer; an invalid queue message is dropped with `ingestion_message_invalid`, and a Durable client failure is logged once as `job_failed` and raised as `DispatcherUnavailableError` with no cause attached; a forced Extract failure leaves no directory under `/run/docchat-scratch`; the client timeouts of item 7 are asserted; a full run with a canary file name and canary text leaves neither in logs or spans; the outbound-client check rejects a client built without timeouts.

## References

- Functions `host.json` reference (`functionTimeout`, logging): https://learn.microsoft.com/en-us/azure/azure-functions/functions-host-json (checked 2026-10-14)
- Functions hosting options (Premium timeout default and maximum): https://learn.microsoft.com/en-us/azure/azure-functions/functions-scale (checked 2026-10-14)
- Durable Functions `host.json` settings (`traceInputsAndOutputs`): https://learn.microsoft.com/en-us/azure/durable-task/durable-functions/durable-functions-host-json-settings (checked 2026-10-14)
- Eternal orchestrations (an unhandled exception fails the orchestration): https://learn.microsoft.com/en-us/azure/durable-task/common/durable-task-eternal-orchestrations (checked 2026-10-14)
- azure-core transport and retry defaults: https://learn.microsoft.com/en-us/python/api/overview/azure/core-readme?view=azure-python (checked 2026-10-21)
- Python 3.13 `asyncio.timeout` and `Timeout.expired()`: https://docs.python.org/3.13/library/asyncio-task.html (checked 2026-10-21)
