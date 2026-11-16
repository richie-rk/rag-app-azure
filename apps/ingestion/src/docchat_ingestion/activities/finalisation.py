"""Sweep duty 9, run finalisation (`14-ingestion.md` sections 6.13, 8.1 and 5.3 duty 9; ADR 0034).

A run with no `queued` or `running` item (an `index_rebuild` run only once no `building` physical
index names it as `build_run_id`, so items added by Flips count in its totals) ends in its own
transaction: its totals and end state, then the logical index status its kind sets, guarded by the
status it expects, so a run that ends during a re-ingest or rebuild never ends that status early.
One failed document never fails its run: the run ends `failed` only when nothing succeeded or was
skipped (cancelled items count with skipped ones).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final
from uuid import UUID

from sqlalchemy import Connection, exists, func, select, update

from docchat_core.ingestion.models import IndexStatus, ItemOperation, RunKind, RunState
from docchat_core.ingestion.runs import compute_index_status
from docchat_core.sql.tables.ingestion import (
    ingestion_run_items,
    ingestion_runs,
    physical_indexes,
    search_indexes,
)
from docchat_core.sql.tables.schema import SYSUTCDATETIME
from docchat_ingestion.activities.support import literal, literals
from docchat_ingestion.outcomes import run_end_state

FINALISATION_BATCH: Final = 200

_items = ingestion_run_items
_runs = ingestion_runs

_COMPUTED_FROM: Final[Mapping[RunKind, tuple[IndexStatus, ...]]] = {
    RunKind.INDEX_REINGEST: (IndexStatus.REINGESTING,),
    RunKind.INDEX_REBUILD: (IndexStatus.REBUILDING,),
    RunKind.DOCUMENT_REINGEST: (IndexStatus.READY, IndexStatus.REINGEST_REQUIRED),
    RunKind.UPLOAD: (IndexStatus.READY, IndexStatus.REINGEST_REQUIRED),
}
"""The kinds whose finalisation sets `compute_index_status()`, and the statuses it replaces."""


@dataclass(frozen=True, slots=True)
class FinalisableRun:
    id: UUID
    kind: RunKind
    search_index_id: UUID
    traceparent: str | None


@dataclass(frozen=True, slots=True)
class FinishedRun:
    run_id: UUID
    kind: RunKind
    state: RunState
    total: int
    succeeded: int
    skipped: int
    failed: int
    cancelled: int
    duration_seconds: float
    traceparent: str | None


def finalisable_runs(connection: Connection) -> list[FinalisableRun]:
    """Open runs with no open item, oldest first, at most 200."""
    open_items = exists().where(
        _items.c.run_id == _runs.c.id, _items.c.state.in_(literals(("queued", "running")))
    )
    building = exists().where(
        physical_indexes.c.build_run_id == _runs.c.id,
        physical_indexes.c.state == literal("building"),
    )
    rows = connection.execute(
        select(_runs.c.id, _runs.c.kind, _runs.c.search_index_id, _runs.c.traceparent)
        .where(
            _runs.c.state.in_(literals(("queued", "running"))),
            ~open_items,
            (_runs.c.kind != literal(RunKind.INDEX_REBUILD)) | ~building,
        )
        .order_by(_runs.c.created_at)
        .limit(FINALISATION_BATCH)
    ).all()
    return [
        FinalisableRun(row.id, RunKind(row.kind), row.search_index_id, row.traceparent)
        for row in rows
    ]


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _index_status(connection: Connection, run: FinalisableRun) -> None:
    index_id = run.search_index_id
    expected: tuple[IndexStatus, ...]
    if run.kind in _COMPUTED_FROM:
        expected = _COMPUTED_FROM[run.kind]
        status = compute_index_status(connection, index_id)
    elif run.kind is RunKind.INDEX_DELETE:
        expected, status = (IndexStatus.DELETING,), IndexStatus.DELETED
    elif run.kind is RunKind.INDEX_CREATE:
        create_failed = connection.execute(
            select(_items.c.id)
            .where(
                _items.c.run_id == run.id,
                _items.c.operation == literal(ItemOperation.CREATE_PHYSICAL_INDEX),
                _items.c.state == literal("failed"),
            )
            .limit(1)
        ).first()
        if create_failed is None:
            return
        expected, status = (IndexStatus.PROVISIONING,), IndexStatus.FAILED
    else:
        return
    connection.execute(
        update(search_indexes)
        .where(
            search_indexes.c.id == index_id,
            search_indexes.c.status.in_(literals(expected)),
        )
        .values(status=status.value, status_changed_at=SYSUTCDATETIME)
    )


def finalise(connection: Connection, run: FinalisableRun) -> FinishedRun | None:
    """One run's finalisation; `None` when another writer ended it first."""
    totals = dict.fromkeys(("succeeded", "skipped", "failed", "cancelled"), 0)
    total = 0
    for row in connection.execute(
        select(_items.c.state, func.count().label("items"))
        .where(_items.c.run_id == run.id)
        .group_by(_items.c.state)
    ).all():
        total += int(row.items)
        if row.state in totals:
            totals[row.state] = int(row.items)
    state = run_end_state(
        succeeded=totals["succeeded"],
        skipped=totals["skipped"],
        failed=totals["failed"],
        cancelled=totals["cancelled"],
    )
    ended = connection.execute(
        update(_runs)
        .where(_runs.c.id == run.id, _runs.c.state.in_(literals(("queued", "running"))))
        .values(
            state=state.value,
            total_items=total,
            succeeded_items=totals["succeeded"],
            skipped_items=totals["skipped"],
            failed_items=totals["failed"],
            cancelled_items=totals["cancelled"],
            completed_at=SYSUTCDATETIME,
        )
        .returning(_runs.c.created_at, _runs.c.completed_at)
    ).first()
    if ended is None:
        return None
    _index_status(connection, run)
    duration = (_as_utc(ended.completed_at) - _as_utc(ended.created_at)).total_seconds()
    return FinishedRun(
        run_id=run.id,
        kind=run.kind,
        state=state,
        total=total,
        succeeded=totals["succeeded"],
        skipped=totals["skipped"],
        failed=totals["failed"],
        cancelled=totals["cancelled"],
        duration_seconds=max(0.0, duration),
        traceparent=run.traceparent,
    )
