# ADR 0007: API process model and shutdown drain

- **Status:** Accepted
- **Date:** 2026-10-18
- **Decided by:** Product owner and architect
- **Specified in:** 01-repository.md, 11-api.md, 12-chat-pipeline.md, 81-config.md, 83-infrastructure.md, 84-testing.md, 85-observability.md, 86-operations.md
- **Related ADRs:** 0006, 0010, 0023, 0055, 0057, 0070, 0081, 0124, 0134, 0148

## Context

The product owner chose Gunicorn as the API's process manager with Uvicorn workers, and NDJSON streaming in which every stream ends with exactly one terminal event, `done` or `error`; a stream that ends without one is a failure the client must show. The worker count must follow the instance's cores and be set in configuration. Stopping one instance must fail only the streams it held. A chat turn runs for at most `chat.max_turn_seconds` (default 180 s, range 60 to 220, ADR 0070), so deploys, restarts and scale-in regularly stop an instance while turns are mid-answer.

Platform and library facts:

- App Service waits `WEBSITES_CONTAINER_STOP_TIME_LIMIT` for a container to stop gracefully after its stop signal: default 5 s, maximum 120 s; then it kills the container (checked 2026-10-15). No graceful timeout longer than 120 s can take effect.
- The App Service front end ends a request that gets no response for about 240 s on Linux, and the app-settings reference describes a 230-second idle request timeout (checked 2026-10-14).
- The worker class bundled in `uvicorn` (`uvicorn.workers`) is deprecated and will be removed; the `uvicorn-worker` package provides `uvicorn_worker.UvicornWorker`. uvicorn-worker 0.4.0 and uvicorn 0.54.0 were current on 2026-10-14.
- On SIGTERM Uvicorn stops accepting connections at once but runs the application's lifespan shutdown only after every open connection has closed. Drain logic placed in lifespan shutdown would wait for the very streams it must end, and the platform would kill the container first.
- A shell as process 1 doesn't forward SIGTERM to its child process.

## Decision

1. **Command.** The API image ends with `CMD ["gunicorn", "--config", "/app/gunicorn.conf.py", "docchat_api.app:create_app()"]` in exec form, so Gunicorn is process 1 and receives the platform's SIGTERM; `create_app` is the composition root (Gunicorn's application-factory form). The site's `appCommandLine` is empty, so the image's own command runs.
2. **`apps/api/gunicorn.conf.py`.** `bind = '0.0.0.0:8000'`; `workers` = `DOCCHAT_API_WORKERS`; `worker_class = 'docchat_api.http.worker.DocChatUvicornWorker'`; `graceful_timeout` = `DOCCHAT_API_GRACEFUL_TIMEOUT_SECONDS` (110, range 30 to 110); `timeout` = `DOCCHAT_API_WORKER_TIMEOUT_SECONDS` (120, range 30 to 120: Gunicorn's worker heartbeat timeout, which never limits a request under the Uvicorn worker); `accesslog = None`, `errorlog = "-"`, `loglevel = "warning"`, `capture_output = False`.
3. **One worker per vCPU.** `DOCCHAT_API_WORKERS` (integer 1 to 16) comes from the SKU map `{B1: 1, P0v3: 1, P1v3: 2, P2v3: 4}` derived in `main.bicep` and is never a free parameter, so a SKU change moves it: 1 in `dev` (B1), 2 in `prod` and `load` (P1v3).
4. **Worker class.** `DocChatUvicornWorker` subclasses `uvicorn_worker.UvicornWorker` (uvicorn-worker 0.4.0); its `CONFIG_KWARGS` set `timeout_keep_alive = 65` (above the 60-second idle timeout common to HTTP proxies, so the front end never reuses a connection the worker has just closed; a 30-minute deployed soak asserts zero `502` responses, and a `502` there raises the value to 245 s), `server_header=False` and `access_log=False`. It serves through `DocChatServer`, a subclass of `uvicorn.Server` (uvicorn 0.54.0) in the same module: `serve()` stores the running event loop in `self.loop`; `handle_exit(sig, frame)` on the first SIGTERM schedules `docchat_api.chat.tasks.begin_drain()` with `self.loop.call_soon_threadsafe` (the function is looked up on its module when the signal arrives), then calls the base `handle_exit`; a later SIGTERM schedules nothing, and SIGINT (a developer's Ctrl+C) schedules no drain. `DocChatUvicornWorker._serve()` is uvicorn-worker 0.4.0's `_serve()` with `DocChatServer` in place of `uvicorn.Server`. Gunicorn's master forwards the container's SIGTERM to every worker as a graceful stop, so deploys, restarts and scale-in all start the drain the same way. `call_soon_threadsafe` is required because a signal handler interrupts the loop's own thread, where `call_soon` isn't safe, and it wakes the loop's selector, so the drain timer starts within milliseconds of the signal.
5. **Drain.** `begin_drain()` is a plain function, never a coroutine function: on the loop thread it records the signal time, arms the drain point with `loop.call_later(90, …)` and returns at once; a later call does nothing; a call before lifespan start-up has finished arms the same timer, which at the drain point finds whatever the task registry holds by then. At the drain point:
   - each registered turn task still running is ended: a turn already finalised `completed` cancels its follow-ups call and writes `done`; any other turn is set `failed` with `error.code` `service_restarting`, writes `error` `service_restarting` (retryable, `retryAfterSeconds` 5) when its stream is still open, and is cancelled, and its shielded finaliser (budget 15 s) stores it; `docchat.chat.drained_turns` counts each;
   - every registered background task (post-turn summary and memory work) is cancelled; the next turn-start re-check recovers its work once its lease expires;
   - every running citation download is aborted with reason `drain` and its lease released, counted by `docchat.citation.download_aborts`;
   - one `chat_drained` warning per worker records `docchat.chat.turn_tasks`, `docchat.chat.background_tasks` and `docchat.chat.downloads`.
6. **Timeline after the stop signal.** 0 s: Gunicorn and Uvicorn stop accepting connections; open streams continue. 90 s: drain point. 105 s at the latest: every turn is saved. 110 s: Gunicorn's graceful timeout ends any worker still running. 120 s: App Service kills the container.
7. **Platform settings on the API site.** `WEBSITES_CONTAINER_STOP_TIME_LIMIT` = 120 (the platform maximum), `WEBSITES_PORT` = 8000 (the port Gunicorn binds), `WEBSITES_CONTAINER_START_TIME_LIMIT` = 300, `WEBSITE_HEALTHCHECK_MAXPINGFAILURES` = 3, health check path `/healthz` (ADR 0023), `alwaysOn: true`, client affinity off.
8. **Pinned behaviour.** `uv.lock` pins Gunicorn 26.2.0, uvicorn 0.54.0 and uvicorn-worker 0.4.0 exactly; an update merges only when the stop-signal tests below pass.

## Alternatives considered

- **`uvicorn.workers.UvicornWorker`.** Lost: deprecated and scheduled for removal.
- **Uvicorn alone with `--workers`.** Lost: the product owner chose Gunicorn as the process manager, whose master restarts a failed worker and forwards the stop signal to each worker.
- **A 240-second graceful timeout matching the turn cap and the front end's idle limit.** Lost: App Service kills the container 120 s after the signal, so Gunicorn would never reach the end of its timeout, and a long turn would end with no terminal event and no saved state.
- **Starting the drain in the lifespan shutdown hook.** Lost: Uvicorn runs that hook only after every connection has closed, so the drain would start after the streams it must end, or never, once the 120-second kill arrives.
- **A shell-form `CMD` or a start-up script.** Lost: `/bin/sh` as process 1 doesn't forward SIGTERM, so no worker would start its drain.
- **More workers than vCPUs.** Lost: each worker runs one event loop, and with I/O awaited a loop needs no extra process to wait; every extra worker adds its own memory, client set and SQL pool (8 threads by default) without adding CPU.
- **No drain, letting the platform kill end the streams.** Lost: streams would end with no terminal event, turns would stay `streaming` until their deadline and then read as `turn_abandoned`, and users would see a failure with no code to act on.

## Consequences

- A turn that finishes within 90 s of the stop signal ends normally; a turn still running at 90 s ends with the retryable `service_restarting` (5 s) and is saved `failed`, so the user asks again on any surviving instance. Every stream keeps its single terminal event, which the statelessness load scenario S2 checks (ADR 0134).
- Obligations: the settings of items 2, 3 and 7 in 81-config.md, and these tests (84-testing.md):
  - unit: with `DocChatServer.loop` set to the test's loop and `begin_drain` replaced by a recorder, `handle_exit(SIGTERM)` runs it once on the next loop iteration, a second SIGTERM adds no call, SIGINT adds none, and `should_exit` is set each time;
  - integration: Gunicorn in a subprocess serving a probe stream delivers the drain line within 1 s of SIGTERM to the master, before lifespan shutdown, and the master exits with code 0 within 10 s once the probe ends its stream;
  - integration: a stream that would outlast 90 s ends with `error service_restarting` at 90 s ± 2 s, the turn is stored `failed`, `docchat.chat.drained_turns` increments, a registered background task is cancelled and the worker exits before 110 s; a turn already finalised at the drain point ends with `done` and stays `completed`;
  - CI image check: `Config.Cmd` is exactly the exec form of item 1, `Config.Entrypoint` is empty and `worker_class` is the custom class;
  - deployed: a 30-minute soak with zero `502` responses;
  - the statelessness load scenario S2.
- Monitoring: `docchat.chat.drained_turns` rises during deploys and scale-in; a rise outside deploy windows points at unplanned restarts, and the runbook in 86-operations.md checks instance health and the platform's restart events.
- Risk: `DocChatServer` relies on uvicorn's `Server.handle_exit` and on uvicorn-worker's `_serve()`, both internals that an upgrade can change. The exact pins and the stop-signal tests turn such a change into a failing pull request, never into silent turn loss.
- Risk: 120 s is the platform maximum, so the drain point can't move later; a longer turn cap would need an earlier drain point, not a longer timeout.

## References

- App Service app settings reference (`WEBSITES_CONTAINER_STOP_TIME_LIMIT`: default 5 s, maximum 120 s): https://learn.microsoft.com/en-us/azure/app-service/reference-app-settings (checked 2026-10-15)
- App Service request timeout on Linux (about 240 s): https://learn.microsoft.com/en-us/troubleshoot/azure/app-service/web-request-times-out-app-service (checked 2026-10-14)
- Uvicorn deployment (`uvicorn.workers` deprecated; use `uvicorn-worker`): https://uvicorn.dev/deployment/ (checked 2026-10-14)
- App Service health check: https://learn.microsoft.com/en-us/azure/app-service/monitor-instances-health-check (checked 2026-10-14)
