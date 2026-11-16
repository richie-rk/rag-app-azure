# ADR 0035: Dispatcher continue-as-new without save events

- **Status:** Accepted
- **Date:** 2026-10-21
- **Decided by:** Product owner
- **Specified in:** 14-ingestion.md, 01-repository.md, 84-testing.md
- **Related ADRs:** 0009, 0028, 0034, 0036, 0053, 0144

## Context

Ingestion runs through one singleton dispatcher orchestration (`ingestion_dispatcher`, instance ID `docchat-ingestion-dispatcher`) that wakes on a `run_queued` event or a durable timer, sweeps SQL, claims items and runs each as a sub-orchestration, so the global parallelism limit holds across all runs (ADR 0034). It loops for as long as the app runs, and Durable Functions replays an orchestration's whole history every time it wakes. Its history therefore has to stay bounded in both busy and idle periods: an idle dispatcher wakes 24 times a day at the 60-minute idle cadence (ADR 0036), and 1,440 times a day at a 60-second cadence, each wake adding a timer and activity calls to its history.

The usual way to reset an endless orchestration is continue-as-new. Keeping the external events pending at the reset would need a `save_events` option, which the GA Python SDK doesn't have.

Vendor facts (checked 2026-10-14):

- `azure-functions-durable` 1.7.0 (2026-07-30) is the latest stable release. Its `DurableOrchestrationContext.continue_as_new(self, input_: Any)` has no `save_events` or `preserve` parameter; `ContinueAsNewAction` serialises only `actionType` and `input`; the extension's out-of-process shim calls `ContinueAsNew(action.Input)` without `preserveUnprocessedEvents`, whose default `false` discards unprocessed external events.
- 2.0.0b1 to 2.0.0rc3 (latest 2026-10-13) are a separate preview line built on the durabletask runtime ("APIs may change before the stable 2.0.0", Python 3.13 or later); durabletask's `continue_as_new(new_input, *, save_events=False, new_version=None)` is the only Python form with `save_events`.
- Continue-as-new keeps the instance ID, starts a new execution and resets the history; the results of incomplete tasks, pending timers included, are discarded. An orchestrator that loops forever without it accumulates history until it causes performance problems and excessive memory use. Every new work item (activity result, timer, event) replays the orchestrator from the start.
- An event raised before the instance waits for it is buffered; one raised to a missing instance is discarded; delivery is at least once. In the Python SDK, open tasks don't block completion or continue-as-new.

## Decision

1. **Counters.** Each generation of the dispatcher counts, from its start, `wakes` (each return from its `task_any` over the timer, the `run_queued` waiter and the running sub-orchestrations) and `completed` (sub-orchestrations that returned or failed, removed from its running set). An item that the sweep reports requeued or failed after a lease expiry leaves the running set without being counted.
2. **Threshold.** The generation starts draining when `completed >= 100` or `wakes >= 60`, whichever comes first. Both values are constants in code, not settings.
3. **Drain.** While draining, the dispatcher calls no `claim_ingestion_items`; it keeps every other part of its wake: `load_dispatch_settings`, `sweep_ingestion_state` (heartbeat, lease expiry, scan results, stale uploads, repoint, run finalisation), the custom status and the wait on the timer, the `run_queued` waiter and the running sub-orchestrations. The timer follows ADR 0036 (60 seconds while work is outstanding or an item runs, 60 minutes when idle).
4. **Reset.** In the first wake, after the sweep, in which the generation is draining and its running set is empty, the dispatcher calls `context.continue_as_new({"generation": generation + 1, "settings": <the last valid ingestion snapshot, or null>})` and returns. That input is the only argument. The input model is `DispatcherInput {generation: int, settings: IngestionOpsConfig | null}`; `ensure_dispatcher` starts generation 0 with `{"generation": 0}`, and carrying the snapshot keeps the last-valid-snapshot rule of ADR 0028 working across generations.
5. **Events are hints.** A `run_queued` event that is pending at the reset is discarded by the platform, and nothing tries to keep it. No work depends on an event: the API commits every run and its items to SQL before it sends the queue message, and every generation's first wake loads its settings, sweeps and claims before it waits, so a dropped event delays nothing beyond the drain; any later run is found by the next wake's sweep, at most one timer period later.
6. **One instance.** Every generation keeps the instance ID `docchat-ingestion-dispatcher`. `ensure_dispatcher` starts a new instance only when the status is absent or `Completed`, `Failed`, `Terminated` or `Canceled`, so a generation in the middle of its reset counts as running. A new generation has no custom status until its first wake ends, which the keep-alive's watchdog treats as outstanding with the instance's `last_updated_time` (ADR 0036).
7. **SDK line.** The app pins `azure-functions-durable` 1.7.0, the GA 1.7.x line (with `azure-functions` 2.3.0 and `azure-functions-runtime` 1.2.1). The durabletask-based 2.x line, which offers `save_events`, is a preview entry of the GA-over-preview register (ADR 0009) and is reviewed once it is GA; adopting it is a new ADR, and the hint design above needs no preserved events either way.

## Alternatives considered

- **`save_events=True` on the 2.x preview SDK.** Rejected: a preview whose APIs may still change, on the orchestration that carries every ingestion run, for a capability the hint design doesn't need.
- **Reset only after a finished run or 100 completed items.** Rejected: an idle dispatcher would never reset, and its history and replay cost would grow by every wake for as long as nothing is uploaded.
- **Continue as new while sub-orchestrations still run.** Rejected: the reset discards the results of incomplete tasks, so the new generation would not be woken when those items finish, a freed slot would wait for the next timer wake, and the generation's counters would miss them.
- **Reset on elapsed time (for example once a day).** Rejected: a busy day can add thousands of wakes and sub-orchestration events within one generation; counting wakes and completed items bounds the history directly.
- **A timer-triggered function that claims and starts items, with no long-running orchestration.** Rejected: it runs only on its schedule, so neither a finished item nor a `run_queued` event could wake it, and freed slots would wait up to a full period.

## Consequences

- Every generation's history stays bounded: at most 60 wakes, each adding the orchestrator's start and end events, its two or three activity calls, its timer and any events received, plus the start and end of at most 100 sub-orchestrations. Replay time and memory stay flat whether the system is idle or busy. An idle generation lasts about 60 hours (60 wakes of 60 minutes).
- At the drain, free slots stay unused until the last running item ends. At the default parallelism of 1 this costs nothing, because no slot is free while an item runs; at parallelism `p` above 1, up to `p − 1` slots wait for the longest running item once per generation.
- A `run_queued` event raised during the reset is lost by design and costs no work, because SQL is the source of truth (ADR 0034).
- Obligations: a test that continue-as-new happens only after 100 completed items or 60 wakes and only with no running sub-orchestration, with the input `{"generation": n + 1, "settings": <the last valid snapshot>}`; a test that 7 simulated days, idle and then busy without completions, keep every generation's history under 1,000 events, covering both wake cadences; the replay-harness test that claims never exceed the slots, a finished sub-orchestration wakes the loop and the `run_queued` waiter is reused (84-testing.md); a Dependabot update of `azure-functions-durable` passes the dispatcher replay harness before it merges; the register entry of ADR 0009 stays until the 2.x line is GA and reviewed.
- Risk: a drain lasts as long as the longest running item, which for a large PDF can be hours (its parse budget alone allows 7,200 seconds, ADR 0049). The sweep keeps running throughout, so leases, scan results and finalisation still progress and the watchdog sees a fresh `lastSweepAt`.

## References

- `DurableOrchestrationContext` API reference (`continue_as_new(input_)`): https://learn.microsoft.com/en-us/python/api/azure-functions-durable/azure.durable_functions.durableorchestrationcontext?view=azure-python (checked 2026-10-14)
- `azure-functions-durable` 1.7.0 and the 2.x pre-releases: https://pypi.org/project/azure-functions-durable/ (checked 2026-10-14)
- SDK source at v1.7.0 (`continue_as_new`, `ContinueAsNewAction`): https://raw.githubusercontent.com/Azure/azure-functions-durable-python/v1.7.0/azure/durable_functions/models/DurableOrchestrationContext.py and https://raw.githubusercontent.com/Azure/azure-functions-durable-python/v1.7.0/azure/durable_functions/models/actions/ContinueAsNewAction.py (checked 2026-10-14)
- Eternal orchestrations: https://learn.microsoft.com/en-us/azure/azure-functions/durable/durable-functions-eternal-orchestrations and https://learn.microsoft.com/en-us/azure/durable-task/common/durable-task-eternal-orchestrations (checked 2026-10-14)
- Orchestration replay: https://learn.microsoft.com/en-us/azure/durable-task/common/durable-task-orchestrations (checked 2026-10-14)
- External events: https://learn.microsoft.com/en-us/azure/azure-functions/durable/durable-functions-external-events (checked 2026-10-14)
- Durable timers: https://learn.microsoft.com/en-us/azure/durable-task/common/durable-task-timers (checked 2026-10-14)
- `azure-functions-durable` 2.x README (preview): https://github.com/microsoft/durabletask-python/blob/main/azure-functions-durable/README.md (checked 2026-10-14)
