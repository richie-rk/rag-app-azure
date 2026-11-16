# ADR 0132: Event-loop guard

- **Status:** Accepted
- **Date:** 2026-10-21
- **Decided by:** Architect
- **Specified in:** 84-testing.md, 85-observability.md, 81-config.md
- **Related ADRs:** 0006, 0007, 0128, 0129, 0131, 0134, 0137, 0138

## Context

Every I/O call is async and CPU-bound work runs off the event loop (ADR 0006). Tests run with asyncio debug mode and a fixture that fails any test in which a callback blocks the loop for more than 100 ms. The static rules (Ruff's `ASYNC` rules, the banned synchronous clients, mypy's `unused-awaitable`, ADRs 0128 and 0129) catch known blocking calls by name; they can't see CPU-heavy code or a library that blocks inside an `async def`. A blocked loop delays every request its worker holds, which is what the load test's target measures (100 concurrent streams on one instance while a cheap endpoint keeps p95 at or under 200 ms), so production needs the same signal as a metric.

Vendor facts (checked 2026-10-21):

- pytest-asyncio's `asyncio_debug` option "enables asyncio debug mode for the default event loop used by asynchronous tests and fixtures"; `asyncio_mode`, `asyncio_default_fixture_loop_scope` and `asyncio_default_test_loop_scope` are ini options beside it (version 1.4.0 in the lock, checked 2026-10-15).
- Python 3.13, debug mode: "Callbacks taking longer than 100 milliseconds are logged. The `loop.slow_callback_duration` attribute can be used to set the minimum execution duration in seconds that is considered 'slow'"; all of asyncio's logging goes through the `asyncio` logger; a coroutine that is called but never awaited emits a `RuntimeWarning`, and debug mode adds where it was created.

## Decision

1. **Debug mode in every test.** The root pytest configuration sets `asyncio_mode = "auto"`, `asyncio_debug = true`, `asyncio_default_fixture_loop_scope = "function"`, `asyncio_default_test_loop_scope = "function"` and `filterwarnings = ["error"]`, so a coroutine never awaited fails the test that created it.
2. **The guard.** The autouse fixture `blocked_loop_guard` (`docchat_testing.loopguard`, registered by the test plugin, so every pytest test in the repository gets it):
   1. at setup, for any test that runs on an event loop, sets `loop.slow_callback_duration = 0.1` on the running loop;
   2. attaches a handler to the `asyncio` logger that captures each WARNING whose message starts with `Executing ` and contains ` took ` (debug mode's slow-callback record), and each record containing `Task was destroyed but it is pending` or `Task exception was never retrieved`;
   3. at teardown removes the handler and fails the test with `Event loop blocked > 100 ms: <callback> took <seconds> s` (or the task message), listing every occurrence.
   The in-process uvicorn server of the streaming tests runs on the test's own loop, so server code is guarded too. Fixture files are read in synchronous fixtures, never inside coroutines (Ruff `ASYNC230`).
3. **One exemption, for the guard's own tests.** `@pytest.mark.allow_blocking(reason=...)` exempts one test, and only in `tests/support/tests`; the plugin fails a test anywhere else that carries the marker. The guard's meta-tests (pytester) prove that a 150 ms `time.sleep` inside a coroutine fails, a 50 ms one passes, `await asyncio.sleep(0.5)` passes, and a "Task was destroyed but it is pending" record fails.
4. **The production signal.** In each API worker (started in the FastAPI lifespan) and each ingestion worker process (started at its first async invocation), one background task in a lifespan-owned `TaskGroup` loops `t0 = loop.time(); await asyncio.sleep(i); lag = loop.time() - t0 - i`, with `i` = `DOCCHAT_TELEMETRY_LOOP_LAG_INTERVAL_MS` (app setting, integer, default 500, allowed 100 to 5,000), and records the histogram `docchat.runtime.event_loop_lag` in milliseconds with the attribute `service`; it is cancelled at shutdown.
5. **Watching it.** The `capacity` workbook charts the maximum lag per instance in 5-minute bins. Alert A30 `event-loop-lag` (log alert, window PT15M, evaluated every PT5M, severity 3, runbook R07) fires for an instance whose 1-minute maximum lag exceeds 500 ms in at least 5 of those minutes: `AppMetrics | where Name == "docchat.runtime.event_loop_lag" | summarize maxLag = max(Max) by AppRoleName, AppRoleInstance, bin(TimeGenerated, 1m) | where maxLag > 500 | summarize blockedMinutes = count() by AppRoleName, AppRoleInstance | where blockedMinutes >= 5`.
6. **End-to-end proof.** Load scenario S1 (100 concurrent streams on one P1v3 instance with zero errors while the cheap endpoint's p95 stays at or under 200 ms) proves under load what the guard proves per test (ADR 0134).

## Alternatives considered

- **Static rules alone.** Rejected: they catch blocking calls by name, not CPU-heavy code or a library that blocks internally.
- **Warning instead of failing, or a higher threshold.** Rejected: the threshold is 100 ms with a failing test, and a warning is ignored once it becomes routine.
- **An opt-in fixture.** Rejected: a test that forgets to opt in leaves its code unguarded; the guard is autouse.
- **asyncio debug mode in production.** Rejected: it slows every callback and logs callback representations, which can carry content; the sleep-drift probe gives the signal for one sleep per interval.
- **A watchdog thread that samples the loop's stack.** Rejected: an extra dependency and stack captures in production, for a signal the probe already gives.

## Consequences

- A test that blocks the loop for more than 100 ms fails with the callback's name, locally and in CI; the fix is always to move the work to a worker thread or process (ADR 0006), never to exempt the test or raise the threshold.
- Debug mode makes the test suite slower; the guard's cost is one logging handler per test.
- On a loaded CI runner a borderline callback can cross 100 ms; that is treated as a real failure, because the same callback would delay requests in production.
- In production a sustained lag above 500 ms raises a severity 3 alert to runbook R07, read with the `capacity` workbook.
- `DOCCHAT_TELEMETRY_LOOP_LAG_INTERVAL_MS` is part of both apps' exact app-setting set.
- Tests (84-testing.md): the meta-tests of item 3; a unit test proves that the monitor records about 0 ms on an idle loop, at least 150 ms after a 200 ms blocking call, and stops at shutdown; a unit test proves that every alert names an existing runbook; S1 in the load test.

## References

- pytest-asyncio configuration: https://pytest-asyncio.readthedocs.io/en/stable/reference/configuration.html (checked 2026-10-21)
- Developing with asyncio (debug mode, slow callbacks): https://docs.python.org/3.13/library/asyncio-dev.html (checked 2026-10-21)
