"""`claim_ingestion_items`, the atomic claim (`14-ingestion.md` sections 5.6 to 5.8 and 4.8;
ADRs 0034 and 0038).

One transaction counts the running items under `UPDLOCK, HOLDLOCK`, so an orphan's completion and
a claim can't race the slot count, then moves at most the free number of queued candidates to
`running`: priority 0 before 1, then `queue_seq` across every run, at most one per document (and,
for index operations, per physical index), an `ingest` item only once its target is provisioned
(or `retiring` or `deleted`) and its version passes the scan gate, and a `purge_index_blobs` item
only while no other purge and no item of its logical index runs. The filtered unique index
`UX_run_items_running_target` is the hard guard: a violation rolls the claim back and the next
wake claims afresh. The claimed runs move to `running` and their stored trace context goes back
to the dispatcher for each item's input.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Final
from uuid import UUID

import azure.durable_functions as df
from pydantic import Field
from sqlalchemy import CHAR, Connection, and_, cast, exists, func, or_, select, update

from docchat_core.ingestion.models import ActivityResult, ClaimedItem, ItemOperation, RunPriority
from docchat_core.sql.engine import SqlUniqueViolation
from docchat_core.sql.tables.ingestion import (
    document_versions,
    ingestion_dispatcher_state,
    ingestion_run_items,
    ingestion_runs,
    physical_indexes,
)
from docchat_core.sql.tables.schema import BIT_FALSE, SYSUTCDATETIME
from docchat_core.telemetry import set_attributes, start_span
from docchat_ingestion.activities.runtime import IngestionProcess, ProcessProvider, runtime
from docchat_ingestion.activities.support import (
    WireModel,
    dateadd,
    literal,
    literals,
    run_links,
    wire,
)
from docchat_ingestion.boundary import failure_result, run_activity

NAME: Final = "claim_ingestion_items"
DEADLINE_SECONDS: Final = 60
SPAN: Final = "docchat.ingestion.dispatcher.claim"
LEASE_MINUTES: Final = 30
"""A claimed item's first lease covers the gap before its first activity (section 4.8)."""
STATEMENT: Final = "claim_ingestion_items"

_items = ingestion_run_items
_running = ingestion_run_items.alias("r")
_candidate = ingestion_run_items.alias("i")


class ClaimInput(WireModel):
    max_parallel: Annotated[int, Field(ge=1, le=16)]
    generation: Annotated[int, Field(ge=0)]


class ClaimResult(ActivityResult):
    """`{ok, items}` with the `ActivityResult` fields."""

    items: tuple[ClaimedItem, ...] = ()


@dataclass(frozen=True, slots=True)
class _Claimed:
    item_id: UUID
    claim_number: int
    instance_id: str
    operation: str
    run_id: UUID
    priority: int


def running_count(connection: Connection) -> int:
    """The running items, read under `UPDLOCK, HOLDLOCK` until the claim commits."""
    statement = (
        select(func.count())
        .select_from(_items)
        .where(_items.c.state == literal("running"))
        .with_hint(_items, "WITH (UPDLOCK, HOLDLOCK)", "mssql")
    )
    return int(connection.execute(statement).scalar_one())


def candidates_statement(free: int) -> Any:
    """The IDs of at most `free` claimable items, in claim order."""
    i = _candidate
    operation = i.c.operation
    blocked = exists().where(
        _running.c.state == literal("running"),
        or_(
            and_(i.c.document_id.is_not(None), _running.c.document_id == i.c.document_id),
            and_(
                i.c.document_id.is_(None),
                i.c.physical_index_id.is_not(None),
                _running.c.physical_index_id == i.c.physical_index_id,
            ),
            and_(
                operation == literal(ItemOperation.PURGE_INDEX_BLOBS),
                _running.c.operation == literal(ItemOperation.PURGE_INDEX_BLOBS),
            ),
            and_(
                operation == literal(ItemOperation.PURGE_INDEX_BLOBS),
                _running.c.search_index_id == i.c.search_index_id,
            ),
        ),
    )
    not_ingest = operation != literal(ItemOperation.INGEST)
    ranked = (
        select(
            i.c.id,
            i.c.priority,
            i.c.queue_seq,
            func.row_number()
            .over(
                partition_by=func.coalesce(i.c.document_id, i.c.physical_index_id),
                order_by=(i.c.priority, i.c.queue_seq),
            )
            .label("rn"),
        )
        .select_from(
            i.outerjoin(
                document_versions, document_versions.c.id == i.c.document_version_id
            ).outerjoin(physical_indexes, physical_indexes.c.id == i.c.physical_index_id)
        )
        .where(
            i.c.state == literal("queued"),
            or_(i.c.not_before.is_(None), i.c.not_before <= SYSUTCDATETIME),
            or_(
                not_ingest,
                physical_indexes.c.provisioned_at.is_not(None),
                physical_indexes.c.state.in_(literals(("retiring", "deleted"))),
            ),
            or_(
                not_ingest,
                document_versions.c.scan_required == BIT_FALSE,
                document_versions.c.scan_status == literal("clean"),
                document_versions.c.state != literal("pending"),
            ),
            ~blocked,
        )
        .subquery("candidates")
    )
    return (
        select(ranked.c.id)
        .where(ranked.c.rn == 1)
        .order_by(ranked.c.priority, ranked.c.queue_seq)
        .limit(free)
        .subquery("pick")
    )


def claim_statement(free: int) -> Any:
    """Move the picked items to `running` with their next claim number, instance ID and first
    lease; returns the claimed rows."""
    pick = candidates_statement(free)
    next_claim = _items.c.claim_count + 1
    return (
        update(_items)
        .where(_items.c.id == pick.c.id, _items.c.state == literal("queued"))
        .values(
            state=literal("running"),
            claim_count=next_claim,
            instance_id=func.concat(
                "docchat-item-", func.lower(cast(_items.c.id, CHAR(36))), "-", next_claim
            ),
            started_at=func.coalesce(_items.c.started_at, SYSUTCDATETIME),
            lease_expires_at=dateadd("minute", LEASE_MINUTES),
            not_before=None,
        )
        .returning(
            _items.c.id,
            _items.c.claim_count,
            _items.c.instance_id,
            _items.c.operation,
            _items.c.run_id,
            _items.c.priority,
        )
    )


def claim(connection: Connection, max_parallel: int) -> tuple[list[_Claimed], dict[UUID, Any]]:
    """The claim's transaction: the claimed items and their runs' stored trace context."""
    free = max_parallel - running_count(connection)
    if free <= 0:
        return [], {}
    rows = connection.execute(claim_statement(free)).all()
    claimed = [
        _Claimed(
            item_id=row.id,
            claim_number=row.claim_count,
            instance_id=row.instance_id,
            operation=row.operation,
            run_id=row.run_id,
            priority=row.priority,
        )
        for row in rows
    ]
    run_ids = sorted({item.run_id for item in claimed}, key=str)
    traces: dict[UUID, Any] = {}
    if run_ids:
        connection.execute(
            update(ingestion_runs)
            .where(ingestion_runs.c.id.in_(run_ids), ingestion_runs.c.state == literal("queued"))
            .values(
                state=literal("running"),
                started_at=func.coalesce(ingestion_runs.c.started_at, SYSUTCDATETIME),
            )
        )
        traces = {
            row.id: row
            for row in connection.execute(
                select(
                    ingestion_runs.c.id, ingestion_runs.c.traceparent, ingestion_runs.c.tracestate
                ).where(ingestion_runs.c.id.in_(run_ids))
            ).all()
        }
    connection.execute(
        update(ingestion_dispatcher_state)
        .where(ingestion_dispatcher_state.c.id == 1)
        .values(last_claim_at=SYSUTCDATETIME, updated_at=SYSUTCDATETIME)
    )
    return claimed, traces


def claimed_items(claimed: Sequence[_Claimed], traces: dict[UUID, Any]) -> tuple[ClaimedItem, ...]:
    items: list[ClaimedItem] = []
    for item in claimed:
        run = traces.get(item.run_id)
        items.append(
            ClaimedItem(
                item_id=item.item_id,
                claim_number=item.claim_number,
                instance_id=item.instance_id,
                operation=ItemOperation(item.operation),
                priority=RunPriority(item.priority),
                run_id=item.run_id,
                traceparent=None if run is None else run.traceparent,
                tracestate=None if run is None else run.tracestate,
            )
        )
    return tuple(items)


async def claim_items(process: IngestionProcess, request: ClaimInput) -> ClaimResult:
    """Run the claim; a unique-index violation (a race the slot lock didn't stop) claims nothing
    this wake."""
    try:
        claimed, traces = await process.sql.run(
            lambda connection: claim(connection, request.max_parallel),
            name=STATEMENT,
            retry="transient",
        )
    except SqlUniqueViolation:
        return ClaimResult.success()
    return ClaimResult.success(items=claimed_items(claimed, traces))


async def run(payload: object, provider: ProcessProvider = runtime) -> dict[str, Any]:
    """The activity inside its root span, guard and deadline."""

    async def body() -> dict[str, Any]:
        request = ClaimInput.from_wire(payload)
        process = await provider()
        result = await claim_items(process, request)
        span_attributes: dict[str, object] = {"docchat.ingestion.items_claimed": len(result.items)}
        if request.generation >= 1:
            span_attributes["docchat.ingestion.generation"] = request.generation
        set_attributes(span, **span_attributes)
        for link in run_links(item.traceparent for item in result.items):
            span.add_link(link.context)
        return wire(result)

    with start_span(SPAN) as span:
        return await run_activity(NAME, DEADLINE_SECONDS, body, failure_result)


async def claim_ingestion_items(payload: object) -> dict[str, Any]:
    return await run(payload)


bp: Final = df.Blueprint()
bp.activity_trigger(input_name="payload")(claim_ingestion_items)
