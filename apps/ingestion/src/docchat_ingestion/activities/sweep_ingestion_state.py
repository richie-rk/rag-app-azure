"""`sweep_ingestion_state`, the dispatcher's sweep (`14-ingestion.md` sections 5.3 to 5.5, 4.9 item
4 and 6.13; ADRs 0034, 0036, 0038, 0039, 0041 and 0052).

The twelve duties run in order, each in its own short transaction. A duty that fails writes one
warning `sweep_duty_failed` naming the duty and the failure's type, and the next duty still runs;
the activity fails only when every duty failed, so one broken duty never stops the claim. The
sweep opens the root span `docchat.ingestion.dispatcher.sweep`, linked to each run it touches by
the run's stored `traceparent`, because one sweep serves many runs and a span has one parent.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from typing import Annotated, Any, Final
from uuid import UUID

import azure.durable_functions as df
from azure.core.exceptions import AzureError
from pydantic import Field
from sqlalchemy import Connection, select

from docchat_core.ingestion.models import ActivityResult
from docchat_core.ingestion.settings import IngestionOpsConfig
from docchat_core.sql.tables.ingestion import ingestion_runs
from docchat_core.telemetry import (
    LOGGER_NAME,
    emit_event,
    log_event,
    qualified_type_name,
    set_attributes,
    start_span,
)
from docchat_core.telemetry.instruments import counter, gauge
from docchat_ingestion.activities import finalisation, repoint, scan_results, sweep_duties
from docchat_ingestion.activities.runtime import IngestionProcess, ProcessProvider, runtime
from docchat_ingestion.activities.support import WireModel, run_links, wire
from docchat_ingestion.boundary import failure_result, run_activity

NAME: Final = "sweep_ingestion_state"
DEADLINE_SECONDS: Final = 300
SPAN: Final = "docchat.ingestion.dispatcher.sweep"
POISON_PEEK: Final = 32
"""The peek returns at most 32 messages; 32 means 32 or more."""

ITEMS_COMPLETED: Final = counter("docchat.ingestion.items.completed")
POISON_LENGTH: Final = gauge("docchat.ingestion.poison_queue.length")

logger: Final = logging.getLogger(LOGGER_NAME)


class SweepInput(WireModel):
    generation: Annotated[int, Field(ge=0)]
    settings: IngestionOpsConfig


class QueuedCounts(WireModel):
    interactive: Annotated[int, Field(ge=0)]
    bulk: Annotated[int, Field(ge=0)]


class SweepResult(ActivityResult):
    """`{ok, sweptAt, requeuedItemIds, failedItemIds, hasOutstandingWork, queued, running,
    oldestQueuedAgeSeconds}` with the `ActivityResult` fields; a duty that failed leaves its
    fields null (and `hasOutstandingWork` true, so the dispatcher keeps its active cadence)."""

    swept_at: datetime | None = None
    requeued_item_ids: tuple[UUID, ...] = ()
    failed_item_ids: tuple[UUID, ...] = ()
    has_outstanding_work: bool = True
    queued: QueuedCounts | None = None
    running: Annotated[int, Field(ge=0)] | None = None
    oldest_queued_age_seconds: Annotated[int, Field(ge=0)] | None = None


@dataclass(slots=True)
class _Sweep:
    """What the duties of one sweep found."""

    attempted: int = 0
    failed: int = 0
    swept_at: datetime | None = None
    requeued: tuple[UUID, ...] = ()
    failed_items: tuple[UUID, ...] = ()
    counts: sweep_duties.Counts | None = None
    runs_finalised: int = 0
    touched_runs: list[UUID] = field(default_factory=list)
    traceparents: dict[UUID, str | None] = field(default_factory=dict)


async def _duty(sweep: _Sweep, name: str, work: Callable[[], Awaitable[None]]) -> None:
    sweep.attempted += 1
    try:
        await work()
    except Exception as failure:  # noqa: BLE001 - recorded once as the duty's warning
        sweep.failed += 1
        log_event(
            logger,
            "sweep_duty_failed",
            logging.WARNING,
            **{"docchat.ingestion.duty": name, "error.type": qualified_type_name(type(failure))},
        )


async def _heartbeat(process: IngestionProcess, sweep: _Sweep, generation: int) -> None:
    sweep.swept_at = await process.sql.run(
        lambda connection: sweep_duties.heartbeat(connection, generation),
        name="sweep_heartbeat",
        retry="transient",
    )


async def _expired_leases(process: IngestionProcess, sweep: _Sweep, max_claims: int) -> None:
    expired = await process.sql.run(
        lambda connection: sweep_duties.expire_leases(connection, max_claims),
        name="sweep_expired_leases",
        retry="transient",
    )
    sweep.requeued = expired.requeued
    sweep.failed_items = tuple(item.id for item in expired.failed)
    sweep.touched_runs.extend(expired.run_ids)
    for item in expired.failed:
        ITEMS_COMPLETED.add(
            1,
            operation=item.operation.value,
            state="failed",
            outcome_code=sweep_duties.PROCESSING_INTERRUPTED,
        )
        properties: dict[str, object] = {
            "itemId": str(item.id),
            "runId": str(item.run_id),
            "operation": item.operation.value,
            "outcomeCode": sweep_duties.PROCESSING_INTERRUPTED,
        }
        if item.stage is not None:
            properties["stage"] = item.stage
        emit_event("ingestion_item_failed", **properties)


async def _scan_results(
    process: IngestionProcess, sweep: _Sweep, settings: IngestionOpsConfig
) -> None:
    sweep.touched_runs.extend(
        await scan_results.resolve_versions(process, settings.scan_wait_minutes)
    )


async def _stale_uploads(process: IngestionProcess, settings: IngestionOpsConfig) -> None:
    uploads = await process.sql.run(
        lambda connection: sweep_duties.stale_uploads(connection, settings.stale_upload_hours),
        name="sweep_stale_uploads",
        retry="transient",
    )
    for upload in uploads:
        try:
            await scan_results.delete_absent_ok(process.documents, upload.blob_path)
        except AzureError:
            # The reservation stays recorded, so the next wake deletes the blob first.
            continue
        await process.sql.run(
            partial(sweep_duties.remove_stale_upload, upload=upload),
            name="sweep_remove_stale_upload",
            retry="transient",
        )


async def _repoint(process: IngestionProcess) -> None:
    indexes = await process.sql.run(
        repoint.rebuilding_indexes, name="sweep_rebuilding_indexes", retry="transient"
    )
    for index in indexes:
        missing = await process.sql.run(
            partial(
                repoint.missing_documents, index_id=index.index_id, building_id=index.building_id
            ),
            name="sweep_repoint_condition",
            retry="transient",
        )
        if missing:
            continue
        done = await process.sql.run(
            partial(repoint.repoint, index=index),
            name="sweep_repoint",
            retry="none",
        )
        if done is None:
            continue
        properties: dict[str, object] = {
            "indexId": str(done.index_id),
            "newPhysicalIndexId": str(done.new_physical_index_id),
            "generation": done.generation,
        }
        if done.old_physical_index_id is not None:
            properties["oldPhysicalIndexId"] = str(done.old_physical_index_id)
        emit_event("index_repointed", **properties)


async def _finalisation(process: IngestionProcess, sweep: _Sweep) -> None:
    runs = await process.sql.run(
        finalisation.finalisable_runs, name="sweep_finalisable_runs", retry="transient"
    )
    for run in runs:
        finished = await process.sql.run(
            partial(finalisation.finalise, run=run),
            name="sweep_finalise_run",
            retry="transient",
        )
        if finished is None:
            continue
        sweep.runs_finalised += 1
        sweep.traceparents[finished.run_id] = finished.traceparent
        emit_event(
            "ingestion_run_finished",
            runId=str(finished.run_id),
            kind=finished.kind.value,
            state=finished.state.value,
            itemsTotal=finished.total,
            itemsSucceeded=finished.succeeded,
            itemsSkipped=finished.skipped,
            itemsFailed=finished.failed,
            itemsCancelled=finished.cancelled,
            durationSeconds=finished.duration_seconds,
        )


async def _counts(process: IngestionProcess, sweep: _Sweep) -> None:
    sweep.counts = await process.sql.run(
        sweep_duties.counts, name="sweep_counts", retry="transient"
    )


async def _poison_gauge(process: IngestionProcess) -> None:
    messages = await process.poison_queue.peek_messages(max_messages=POISON_PEEK)
    POISON_LENGTH.set(len(list(messages)))


def _traceparents(connection: Connection, run_ids: list[UUID]) -> dict[UUID, str | None]:
    rows = connection.execute(
        select(ingestion_runs.c.id, ingestion_runs.c.traceparent).where(
            ingestion_runs.c.id.in_(run_ids)
        )
    ).all()
    return {row.id: row.traceparent for row in rows}


async def sweep_state(process: IngestionProcess, request: SweepInput) -> tuple[SweepResult, _Sweep]:
    """Run the duties in order; the result and what the duties found."""
    settings = request.settings
    sweep = _Sweep()
    await _duty(sweep, "heartbeat", lambda: _heartbeat(process, sweep, request.generation))
    await _duty(
        sweep, "expired_leases", lambda: _expired_leases(process, sweep, settings.max_item_claims)
    )
    await _duty(sweep, "scan_results", lambda: _scan_results(process, sweep, settings))
    await _duty(sweep, "stale_uploads", lambda: _stale_uploads(process, settings))
    await _duty(
        sweep,
        "orphaned_versions",
        lambda: _created(
            process,
            "sweep_orphaned_versions",
            lambda connection: sweep_duties.queue_orphaned_versions(
                connection, settings.orphan_pending_minutes
            ),
        ),
    )
    await _duty(
        sweep,
        "leftover_cleanup",
        lambda: _created(
            process,
            "sweep_leftover_cleanup",
            lambda connection: sweep_duties.queue_leftover_cleanup(
                connection, settings.cleanup_retry_minutes
            ),
        ),
    )
    await _duty(sweep, "repoint", lambda: _repoint(process))
    await _duty(
        sweep,
        "due_deletions",
        lambda: _created(process, "sweep_due_deletions", sweep_duties.queue_due_deletions),
    )
    await _duty(sweep, "finalisation", lambda: _finalisation(process, sweep))
    await _duty(sweep, "counts", lambda: _counts(process, sweep))
    await _duty(sweep, "poison_gauge", lambda: _poison_gauge(process))
    if process.scanning_enabled:
        await _duty(
            sweep,
            "scanner_canaries",
            lambda: scan_results.resolve_canaries(process, settings.scan_wait_minutes),
        )
    await _duty(
        sweep,
        "stale_renditions",
        lambda: _created(
            process,
            "sweep_stale_renditions",
            lambda connection: sweep_duties.queue_stale_renditions(
                connection, process.rendition_rules_version
            ),
        ),
    )
    if sweep.failed == sweep.attempted:
        return SweepResult.failure("internal_error"), sweep
    counts = sweep.counts
    result = SweepResult.success(
        swept_at=sweep.swept_at,
        requeued_item_ids=sweep.requeued,
        failed_item_ids=sweep.failed_items,
        has_outstanding_work=True if counts is None else counts.has_outstanding_work,
        queued=None if counts is None else QueuedCounts.model_validate(counts.queued),
        running=None if counts is None else counts.running,
        oldest_queued_age_seconds=None if counts is None else counts.oldest_queued_age_seconds,
    )
    return result, sweep


async def _created(
    process: IngestionProcess, statement: str, work: Callable[[Connection], list[UUID]]
) -> None:
    # Run creation draws random IDs, so the unit is never re-run as a whole.
    await process.sql.run(work, name=statement, retry="none")


async def _link_runs(process: IngestionProcess, sweep: _Sweep) -> list[str | None]:
    pending = [
        run_id for run_id in dict.fromkeys(sweep.touched_runs) if run_id not in sweep.traceparents
    ]
    if pending:
        # Links are best effort: the sweep's work is done whether or not they can be read.
        with contextlib.suppress(Exception):
            sweep.traceparents |= await process.sql.run(
                lambda connection: _traceparents(connection, pending),
                name="sweep_run_traces",
                retry="transient",
            )
    return list(sweep.traceparents.values())


async def run(payload: object, provider: ProcessProvider = runtime) -> dict[str, Any]:
    """The activity inside its root span, guard and deadline."""

    async def body() -> dict[str, Any]:
        request = SweepInput.from_wire(payload)
        process = await provider()
        result, sweep = await sweep_state(process, request)
        attributes: dict[str, object] = {
            "docchat.ingestion.items_requeued": len(sweep.requeued),
            "docchat.ingestion.runs_finalised": sweep.runs_finalised,
        }
        if request.generation >= 1:
            attributes["docchat.ingestion.generation"] = request.generation
        set_attributes(span, **attributes)
        for link in run_links(await _link_runs(process, sweep)):
            span.add_link(link.context)
        return wire(result)

    with start_span(SPAN) as span:
        return await run_activity(NAME, DEADLINE_SECONDS, body, failure_result)


async def sweep_ingestion_state(payload: object) -> dict[str, Any]:
    return await run(payload)


bp: Final = df.Blueprint()
bp.activity_trigger(input_name="payload")(sweep_ingestion_state)
