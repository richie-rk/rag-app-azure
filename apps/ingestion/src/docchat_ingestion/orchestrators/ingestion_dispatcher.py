"""`ingestion_dispatcher`, the singleton claimer of work (`14-ingestion.md` section 5; ADRs 0034 to
0039).

One instance, `docchat-ingestion-dispatcher`, kept by every generation. Each wake loads the
`ingestion` settings, sweeps, claims up to the free slots and starts one sub-orchestration per
claimed item, sets its custom status and waits for the first of a timer, a `run_queued` event or a
finished sub-orchestration. Slots are counted in SQL by the claim, never here, so the limit holds
across restarts. The function is a deterministic generator: it does no I/O, reads time only from
the context, generates no IDs, writes no logs or spans, and raises only first-party exceptions with
constant messages; every activity it calls runs inside the failure boundary, so a failure arrives
as a result with `ok: false`, and the dispatcher then waits 60 seconds and tries at its next wake.

A generation drains after 100 completed sub-orchestrations or 60 wakes: it claims nothing more,
and at the first wake after the sweep with nothing running it continues as new with the next
generation number and its last valid settings snapshot, the only argument the GA SDK takes. A
`run_queued` event pending at that moment is dropped by the platform, which delays nothing, because
every generation's first wake sweeps and claims before it waits.
"""

from __future__ import annotations

from collections.abc import Generator, Mapping, Sequence
from datetime import timedelta
from typing import Any, Final

import azure.durable_functions as df
from pydantic import ValidationError

from docchat_core.ingestion.models import (
    DISPATCHER_INSTANCE_ID,
    DOCUMENT_OPERATIONS,
    ClaimedItem,
    DispatcherInput,
    ItemInput,
    RunPriority,
)
from docchat_core.ingestion.settings import IngestionOpsConfig
from docchat_ingestion.orchestrators.retry import OrchestrationContext

NAME: Final = "ingestion_dispatcher"
INSTANCE_ID: Final = DISPATCHER_INSTANCE_ID
RUN_QUEUED: Final = "run_queued"
LOAD_SETTINGS: Final = "load_dispatch_settings"
SWEEP: Final = "sweep_ingestion_state"
CLAIM: Final = "claim_ingestion_items"
DOCUMENT_ORCHESTRATOR: Final = "document_item_orchestrator"
INDEX_ORCHESTRATOR: Final = "index_item_orchestrator"

COMPLETED_LIMIT: Final = 100
WAKES_LIMIT: Final = 60
RETRY_WAKE_SECONDS: Final = 60
"""The wait after a wake with no valid settings or a failed sweep."""


class DispatcherInvariantError(Exception):
    """A broken first-party invariant (an input or a recorded result of the wrong shape). It
    fails the instance, and the keep-alive starts a new one within 5 minutes; the constant
    message keeps every value out of Durable history."""

    def __init__(self) -> None:
        super().__init__("dispatcher invariant broken")


def _validated[M: (DispatcherInput, IngestionOpsConfig, ClaimedItem)](
    model: type[M], data: object
) -> M:
    try:
        return model.model_validate(data, by_alias=True, by_name=False)
    except ValidationError:
        raise DispatcherInvariantError from None


def snapshot_input(settings: IngestionOpsConfig | None) -> dict[str, Any] | None:
    """The snapshot as orchestration input: its dotted keys (section 5.2)."""
    return None if settings is None else settings.model_dump(mode="json")


def orchestrator_for(claim: ClaimedItem) -> str:
    """The sub-orchestration of an item's operation (section 4.2)."""
    return DOCUMENT_ORCHESTRATOR if claim.operation in DOCUMENT_OPERATIONS else INDEX_ORCHESTRATOR


def custom_status(
    *,
    generation: int,
    last_sweep_at: str | None,
    sweep: Mapping[str, Any] | None,
    claimed: Sequence[ClaimedItem],
    running: int,
    config_valid: bool,
) -> dict[str, Any]:
    """The wake's custom status, built only from recorded activity outputs so a replay sets the
    same value (section 5.9 item 4). `sweep` is this wake's successful sweep, or `None`."""
    if sweep is None:
        return {
            "generation": generation,
            "lastSweepAt": last_sweep_at,
            "outstanding": True,
            "queued": None,
            "running": None,
            "oldestQueuedAgeSeconds": None,
            "configValid": config_valid,
        }
    counted = sweep.get("queued")
    queued: dict[str, int] | None = None
    if isinstance(counted, Mapping):
        queued = {}
        for priority in RunPriority:
            claimed_here = sum(1 for claim in claimed if claim.priority is priority)
            queued[priority.label] = max(0, int(counted.get(priority.label, 0)) - claimed_here)
    swept_running = sweep.get("running")
    return {
        "generation": generation,
        "lastSweepAt": last_sweep_at,
        "outstanding": bool(sweep.get("hasOutstandingWork")) or running > 0,
        "queued": queued,
        "running": None if swept_running is None else int(swept_running) + len(claimed),
        "oldestQueuedAgeSeconds": sweep.get("oldestQueuedAgeSeconds"),
        "configValid": config_valid,
    }


def _cancel(timer: Any) -> None:
    # The SDK's timer task has `cancel()`; a task double without it simply isn't cancelled.
    cancel = getattr(timer, "cancel", None)
    if callable(cancel):
        cancel()


def _successful(output: object) -> Mapping[str, Any] | None:
    """A recorded activity output that reports `ok`, or `None`."""
    return output if isinstance(output, Mapping) and output.get("ok") is True else None


def loaded_settings(
    loaded: object, last_valid: IngestionOpsConfig | None
) -> IngestionOpsConfig | None:
    """The wake's snapshot: a valid loaded one, else the last valid one (section 5.2 item 3)."""
    result = _successful(loaded)
    if result is None or result.get("valid") is not True:
        return last_valid
    return _validated(IngestionOpsConfig, result.get("settings"))


def forget_expired(running: dict[str, Any], sweep: Mapping[str, Any]) -> None:
    """Stop awaiting the orphans whose lease expired; their slots are SQL's to count."""
    for item_id in [*sweep.get("requeuedItemIds", ()), *sweep.get("failedItemIds", ())]:
        running.pop(str(item_id), None)


def claimed_items(claims: object) -> list[ClaimedItem]:
    result = _successful(claims)
    if result is None:
        return []
    return [_validated(ClaimedItem, item) for item in result.get("items", ())]


def wake_delay(
    sweep: Mapping[str, Any] | None, settings: IngestionOpsConfig | None, running: int
) -> int:
    """The timer of section 5.9 item 1."""
    if sweep is None or settings is None:
        return RETRY_WAKE_SECONDS
    if bool(sweep.get("hasOutstandingWork")) or running > 0:
        return settings.active_wake_seconds
    return settings.idle_wake_minutes * 60


def settle(running: dict[str, Any]) -> int:
    """Remove the sub-orchestrations that returned or failed (a failed one is left to its
    lease); the number removed."""
    finished = [item_id for item_id, task in running.items() if task.is_completed]
    for item_id in finished:
        running.pop(item_id)
    return len(finished)


def ingestion_dispatcher(context: OrchestrationContext) -> Generator[Any, Any]:
    """The dispatcher's loop (section 5.1)."""
    given = _validated(DispatcherInput, context.get_input())
    generation, last_valid = given.generation, given.settings
    running: dict[str, Any] = {}
    wakes = completed = 0
    last_sweep_at: str | None = None
    event = context.wait_for_external_event(RUN_QUEUED)
    while True:
        last_valid = loaded_settings((yield context.call_activity(LOAD_SETTINGS, {})), last_valid)
        settings = last_valid
        sweep: Mapping[str, Any] | None = None
        claimed: list[ClaimedItem] = []
        if settings is not None:
            sweep = _successful(
                (
                    yield context.call_activity(
                        SWEEP, {"generation": generation, "settings": snapshot_input(settings)}
                    )
                )
            )
        if sweep is not None:
            # A sweep whose heartbeat duty failed has no time; the last one stays the heartbeat.
            last_sweep_at = sweep.get("sweptAt") or last_sweep_at
            forget_expired(running, sweep)
        draining = completed >= COMPLETED_LIMIT or wakes >= WAKES_LIMIT
        if draining and not running:
            context.continue_as_new(
                {"generation": generation + 1, "settings": snapshot_input(last_valid)}
            )
            return
        if sweep is not None and settings is not None and not draining:
            claimed = claimed_items(
                (
                    yield context.call_activity(
                        CLAIM,
                        {"maxParallel": settings.max_parallel_items, "generation": generation},
                    )
                )
            )
            for claim in claimed:
                running[str(claim.item_id)] = context.call_sub_orchestrator(
                    orchestrator_for(claim),
                    ItemInput.from_claim(claim, settings).model_dump(mode="json"),
                    claim.instance_id,
                )
        delay = wake_delay(sweep, settings, len(running))
        context.set_custom_status(
            custom_status(
                generation=generation,
                last_sweep_at=last_sweep_at,
                sweep=sweep,
                claimed=claimed,
                running=len(running),
                config_valid=settings is not None,
            )
        )
        timer = context.create_timer(context.current_utc_datetime + timedelta(seconds=delay))
        winner = yield context.task_any([timer, event, *running.values()])
        wakes += 1
        if winner is not timer and not timer.is_completed:
            _cancel(timer)
        if winner is event:
            event = context.wait_for_external_event(RUN_QUEUED)
        completed += settle(running)


bp: Final = df.Blueprint()
bp.orchestration_trigger(context_name="context")(ingestion_dispatcher)
