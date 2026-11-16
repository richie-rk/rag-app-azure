"""The SQL of the sweep's duties other than the scan results, the repoint and the run
finalisation (`14-ingestion.md` section 5.3 duties 1, 2, 4, 5, 6, 8, 10 and 12, section 6.11's
failure side effects and section 2.8's maintenance runs).

Each function is one duty's unit of work on the caller's connection. Every write is guarded by the
state it expects, so a duty that races an activity, or runs twice, changes nothing it shouldn't.
Duties that create `maintenance` runs draw random IDs and are never re-run as a whole on a
transient error; the next wake finds whatever is still due.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID

from sqlalchemy import (
    Connection,
    and_,
    case,
    delete,
    exists,
    func,
    literal_column,
    or_,
    select,
    union,
    update,
)

from docchat_core.ingestion.models import ItemOperation, ItemState, RunPriority
from docchat_core.ingestion.runs import NewItem, apply_failure_side_effects, create_maintenance_run
from docchat_core.sql.tables.ingestion import (
    chunk_sets,
    document_versions,
    documents,
    ingestion_dispatcher_state,
    ingestion_run_items,
    physical_indexes,
    search_indexes,
)
from docchat_core.sql.tables.schema import BIT_FALSE, SYSUTCDATETIME
from docchat_ingestion.activities.support import catalogue_message, dateadd, literal, literals

PROCESSING_INTERRUPTED: Final = "processing_interrupted"
STALE_UPLOADS_BATCH: Final = 200
ORPHANED_BATCH: Final = 100
LEFTOVER_BATCH: Final = 100
DUE_DELETIONS_BATCH: Final = 100
DUE_DELETION_GRACE_MINUTES: Final = 15
RERENDER_OUTSTANDING_MAX: Final = 10
RERENDER_RETRY_HOURS: Final = 24
RERENDER_RETRIED_CODES: Final = (
    "storage_unavailable",
    "activity_timeout",
    "processing_interrupted",
)
"""A `rerender` that failed with one of these is queued again 24 hours later; any other code
would repeat on the same bytes and rules."""

_items = ingestion_run_items
_OPEN: Final = ("queued", "running")


def _open_items_for(**columns: Any) -> Any:
    """`EXISTS` an item in state `queued` or `running` matching `columns`."""
    return exists().where(
        *(getattr(_items.c, name) == value for name, value in columns.items()),
        _items.c.state.in_(literals(_OPEN)),
    )


# --- 1. Heartbeat ------------------------------------------------------------------------------


def heartbeat(connection: Connection, generation: int) -> datetime:
    """The dispatcher row's heartbeat; its `last_sweep_at` is the sweep's `sweptAt`."""
    running = (
        select(func.count())
        .select_from(_items)
        .where(_items.c.state == literal("running"))
        .scalar_subquery()
    )
    row = connection.execute(
        update(ingestion_dispatcher_state)
        .where(ingestion_dispatcher_state.c.id == 1)
        .values(
            generation=generation,
            last_sweep_at=SYSUTCDATETIME,
            running_items=running,
            updated_at=SYSUTCDATETIME,
        )
        .returning(ingestion_dispatcher_state.c.last_sweep_at)
    ).first()
    if row is None:
        raise LookupError("the dispatcher row is missing")
    swept: datetime = row.last_sweep_at
    # `DATETIME2(3)` reads back as naive UTC; the keep-alive compares it with an aware clock.
    return swept.replace(tzinfo=UTC) if swept.tzinfo is None else swept


# --- 2. Expired leases -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FailedItem:
    """An item whose lease expired once too often."""

    id: UUID
    run_id: UUID
    operation: ItemOperation
    stage: str | None


@dataclass(frozen=True, slots=True)
class ExpiredLeases:
    requeued: tuple[UUID, ...] = ()
    failed: tuple[FailedItem, ...] = ()
    run_ids: tuple[UUID, ...] = ()


def expire_leases(connection: Connection, max_claims: int) -> ExpiredLeases:
    """Every expired lease: back to `queued` while claims are left, otherwise `failed` with
    `processing_interrupted` and the failure side effects, in the same transaction."""
    with_claims_left = _items.c.claim_count < max_claims
    rows = connection.execute(
        update(_items)
        .where(_items.c.state == literal("running"), _items.c.lease_expires_at < SYSUTCDATETIME)
        .values(
            state=case((with_claims_left, literal("queued")), else_=literal("failed")),
            outcome_code=case((with_claims_left, None), else_=PROCESSING_INTERRUPTED),
            outcome_message=case(
                (with_claims_left, None), else_=catalogue_message(PROCESSING_INTERRUPTED)
            ),
            lease_expires_at=None,
            not_before=case((with_claims_left, SYSUTCDATETIME), else_=None),
            completed_at=case((with_claims_left, None), else_=SYSUTCDATETIME),
        )
        .returning(
            _items.c.id,
            _items.c.state,
            _items.c.run_id,
            _items.c.operation,
            _items.c.stage,
        )
    ).all()
    requeued = tuple(row.id for row in rows if row.state == ItemState.QUEUED)
    failed = tuple(
        FailedItem(
            id=row.id,
            run_id=row.run_id,
            operation=ItemOperation(row.operation),
            stage=row.stage,
        )
        for row in rows
        if row.state == ItemState.FAILED
    )
    for item in failed:
        apply_failure_side_effects(connection, item.id, PROCESSING_INTERRUPTED)
    return ExpiredLeases(requeued, failed, tuple(dict.fromkeys(row.run_id for row in rows)))


# --- 4. Stale uploads --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StaleUpload:
    id: UUID
    document_id: UUID
    blob_path: str
    reserved_at: datetime


def stale_uploads(connection: Connection, stale_hours: int) -> list[StaleUpload]:
    rows = connection.execute(
        select(
            document_versions.c.id,
            document_versions.c.document_id,
            document_versions.c.blob_path,
            document_versions.c.reserved_at,
        )
        .where(
            document_versions.c.state == literal("uploading"),
            document_versions.c.reserved_at < dateadd("hour", -stale_hours),
        )
        .order_by(document_versions.c.reserved_at)
        .limit(STALE_UPLOADS_BATCH)
    ).all()
    return [StaleUpload(row.id, row.document_id, row.blob_path, row.reserved_at) for row in rows]


def remove_stale_upload(connection: Connection, upload: StaleUpload) -> bool:
    """Delete the reservation unless a reserve refreshed it meanwhile, then its document when it
    has no version left and was never current."""
    removed = connection.execute(
        delete(document_versions).where(
            document_versions.c.id == upload.id,
            document_versions.c.state == literal("uploading"),
            document_versions.c.reserved_at == upload.reserved_at,
        )
    ).rowcount
    connection.execute(
        delete(documents).where(
            documents.c.id == upload.document_id,
            documents.c.current_version_id.is_(None),
            documents.c.is_deleted == BIT_FALSE,
            ~exists().where(document_versions.c.document_id == upload.document_id),
        )
    )
    return bool(removed == 1)


# --- Maintenance runs --------------------------------------------------------------------------


def _create_runs(connection: Connection, grouped: dict[UUID, list[NewItem]]) -> list[UUID]:
    """One `maintenance` run per logical index, in a stable order."""
    return [
        create_maintenance_run(connection, index_id=index_id, items=items).run_id
        for index_id, items in sorted(grouped.items(), key=lambda pair: str(pair[0]))
        if items
    ]


def _targets(connection: Connection, index_ids: Iterable[UUID]) -> dict[UUID, UUID]:
    """Each logical index's active physical index or, without one, its building one."""
    ids = sorted(set(index_ids), key=str)
    if not ids:
        return {}
    targets: dict[UUID, UUID] = {}
    for row in connection.execute(
        select(search_indexes.c.id, search_indexes.c.active_physical_index_id).where(
            search_indexes.c.id.in_(ids)
        )
    ).all():
        if row.active_physical_index_id is not None:
            targets[row.id] = row.active_physical_index_id
    missing = [index_id for index_id in ids if index_id not in targets]
    if missing:
        for row in connection.execute(
            select(physical_indexes.c.search_index_id, physical_indexes.c.id).where(
                physical_indexes.c.search_index_id.in_(missing),
                physical_indexes.c.state == literal("building"),
            )
        ).all():
            targets[row.search_index_id] = row.id
    return targets


# --- 5. Orphaned pending versions --------------------------------------------------------------


def queue_orphaned_versions(connection: Connection, orphan_minutes: int) -> list[UUID]:
    """One `maintenance` run per logical index with one `ingest` item per pending version that
    no queued or running item covers (a submit that never happened, a cancelled run)."""
    rows = connection.execute(
        select(
            document_versions.c.id,
            document_versions.c.document_id,
            document_versions.c.search_index_id,
        )
        .where(
            document_versions.c.state == literal("pending"),
            document_versions.c.committed_at < dateadd("minute", -orphan_minutes),
            document_versions.c.scan_status.in_(literals(("not_required", "awaiting", "clean"))),
            ~_open_items_for(document_version_id=document_versions.c.id),
        )
        .order_by(document_versions.c.committed_at)
        .limit(ORPHANED_BATCH)
    ).all()
    targets = _targets(connection, (row.search_index_id for row in rows))
    grouped: dict[UUID, list[NewItem]] = defaultdict(list)
    for row in rows:
        target = targets.get(row.search_index_id)
        if target is None:
            # An index with no physical index to build into has nothing an item can target.
            continue
        grouped[row.search_index_id].append(
            NewItem(
                ItemOperation.INGEST,
                physical_index_id=target,
                document_id=row.document_id,
                document_version_id=row.id,
            )
        )
    return _create_runs(connection, grouped)


# --- 6. Leftover clean-up ----------------------------------------------------------------------


def queue_leftover_cleanup(connection: Connection, cleanup_minutes: int) -> list[UUID]:
    """One `maintenance` run per logical index with one `cleanup_document` item per document
    whose retired chunks or superseded and failed versions' storage are still to remove."""
    retired_before = dateadd("minute", -cleanup_minutes)
    retired_sets = (
        select(chunk_sets.c.document_id, documents.c.search_index_id)
        .select_from(
            chunk_sets.join(
                physical_indexes, physical_indexes.c.id == chunk_sets.c.physical_index_id
            ).join(documents, documents.c.id == chunk_sets.c.document_id)
        )
        .where(
            chunk_sets.c.state == literal("retired"),
            chunk_sets.c.chunks_deleted_at.is_(None),
            chunk_sets.c.retired_at < retired_before,
            physical_indexes.c.state.in_(literals(("building", "active"))),
        )
    )
    unpurged = select(document_versions.c.document_id, document_versions.c.search_index_id).where(
        document_versions.c.storage_purged_at.is_(None),
        or_(
            and_(
                document_versions.c.state == literal("superseded"),
                document_versions.c.superseded_at < retired_before,
            ),
            and_(
                document_versions.c.state == literal("failed"),
                document_versions.c.failed_at < retired_before,
            ),
        ),
    )
    leftover = union(retired_sets, unpurged).subquery("x")
    rows = connection.execute(
        select(leftover.c.document_id, leftover.c.search_index_id)
        .select_from(
            leftover.join(search_indexes, search_indexes.c.id == leftover.c.search_index_id)
        )
        .where(
            search_indexes.c.status.not_in(literals(("deleting", "deleted"))),
            ~exists().where(
                _items.c.document_id == leftover.c.document_id,
                _items.c.operation == literal(ItemOperation.CLEANUP_DOCUMENT),
                _items.c.state.in_(literals(_OPEN)),
            ),
        )
        .limit(LEFTOVER_BATCH)
    ).all()
    grouped: dict[UUID, list[NewItem]] = defaultdict(list)
    for row in rows:
        grouped[row.search_index_id].append(
            NewItem(ItemOperation.CLEANUP_DOCUMENT, document_id=row.document_id)
        )
    return _create_runs(connection, grouped)


# --- 8. Due deletions --------------------------------------------------------------------------


def queue_due_deletions(connection: Connection) -> list[UUID]:
    """One `maintenance` run per logical index with one `delete_physical_index` item per
    `retiring` physical index 15 minutes past its `delete_after` with no open delete item; this
    also retries a delete item that failed."""
    rows = connection.execute(
        select(physical_indexes.c.id, physical_indexes.c.search_index_id)
        .where(
            physical_indexes.c.state == literal("retiring"),
            physical_indexes.c.delete_after < dateadd("minute", -DUE_DELETION_GRACE_MINUTES),
            ~exists().where(
                _items.c.physical_index_id == physical_indexes.c.id,
                _items.c.operation == literal(ItemOperation.DELETE_PHYSICAL_INDEX),
                _items.c.state.in_(literals(_OPEN)),
            ),
        )
        .limit(DUE_DELETIONS_BATCH)
    ).all()
    grouped: dict[UUID, list[NewItem]] = defaultdict(list)
    for row in rows:
        grouped[row.search_index_id].append(
            NewItem(ItemOperation.DELETE_PHYSICAL_INDEX, physical_index_id=row.id)
        )
    return _create_runs(connection, grouped)


# --- 10. Counts --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Counts:
    queued: dict[str, int] = field(default_factory=lambda: {"interactive": 0, "bulk": 0})
    running: int = 0
    oldest_queued_age_seconds: int | None = None
    has_outstanding_work: bool = False


def counts(connection: Connection) -> Counts:
    """The queued items per priority with the oldest one's age, the running items, and whether
    any work is outstanding: an item `queued` or `running`, a version awaiting its scan, or a
    physical index `building` or `retiring`."""
    queued = {"interactive": 0, "bulk": 0}
    ages: list[int] = []
    for row in connection.execute(
        select(
            _items.c.priority,
            func.count().label("items"),
            func.datediff(
                literal_column("second"), func.min(_items.c.created_at), SYSUTCDATETIME
            ).label("age"),
        )
        .where(_items.c.state == literal("queued"))
        .group_by(_items.c.priority)
    ).all():
        queued[RunPriority(row.priority).label] = int(row.items)
        ages.append(max(0, int(row.age)))
    running = int(
        connection.execute(
            select(func.count()).select_from(_items).where(_items.c.state == literal("running"))
        ).scalar_one()
    )
    outstanding = bool(queued["interactive"] or queued["bulk"] or running)
    if not outstanding:
        outstanding = (
            connection.execute(
                select(
                    case(
                        (
                            or_(
                                exists().where(
                                    document_versions.c.state == literal("pending"),
                                    document_versions.c.scan_status == literal("awaiting"),
                                ),
                                exists().where(
                                    physical_indexes.c.state.in_(literals(("building", "retiring")))
                                ),
                            ),
                            1,
                        ),
                        else_=0,
                    )
                )
            ).scalar_one()
            == 1
        )
    return Counts(
        queued=queued,
        running=running,
        oldest_queued_age_seconds=max(ages) if ages else None,
        has_outstanding_work=outstanding,
    )


# --- 12. Stale renditions ----------------------------------------------------------------------


def queue_stale_renditions(connection: Connection, rules_version: int) -> list[UUID]:
    """One bulk `maintenance` run per logical index with one `rerender` item per served chunk set
    whose renditions an earlier `RENDITION_RULES_VERSION` built: at most one per document, at
    most 10 `rerender` items outstanding, none for a document with an open item, and a set whose
    `rerender` failed again only 24 hours after a transient failure (section 6.15)."""
    outstanding = int(
        connection.execute(
            select(func.count())
            .select_from(_items)
            .where(
                _items.c.operation == literal(ItemOperation.RERENDER),
                _items.c.state.in_(literals(_OPEN)),
            )
        ).scalar_one()
    )
    free = RERENDER_OUTSTANDING_MAX - outstanding
    if free <= 0:
        return []
    failed = _items.alias("f")
    ranked = (
        select(
            chunk_sets.c.id,
            chunk_sets.c.document_id,
            chunk_sets.c.document_version_id,
            chunk_sets.c.physical_index_id,
            chunk_sets.c.served_at,
            documents.c.search_index_id,
            func.row_number()
            .over(partition_by=chunk_sets.c.document_id, order_by=chunk_sets.c.served_at)
            .label("rn"),
        )
        .select_from(
            chunk_sets.join(documents, documents.c.id == chunk_sets.c.document_id).join(
                search_indexes, search_indexes.c.id == documents.c.search_index_id
            )
        )
        .where(
            chunk_sets.c.state == literal("served"),
            or_(
                chunk_sets.c.rendition_rules_version.is_(None),
                chunk_sets.c.rendition_rules_version < rules_version,
            ),
            documents.c.is_deleted == BIT_FALSE,
            search_indexes.c.status.not_in(literals(("deleting", "deleted"))),
            ~_open_items_for(document_id=chunk_sets.c.document_id),
            ~exists().where(
                failed.c.document_id == chunk_sets.c.document_id,
                failed.c.chunk_set_id == chunk_sets.c.id,
                failed.c.operation == literal(ItemOperation.RERENDER),
                failed.c.state == literal("failed"),
                or_(
                    failed.c.outcome_code.not_in(literals(RERENDER_RETRIED_CODES)),
                    failed.c.completed_at > dateadd("hour", -RERENDER_RETRY_HOURS),
                ),
            ),
        )
        .subquery("x")
    )
    rows = connection.execute(
        select(
            ranked.c.id,
            ranked.c.document_id,
            ranked.c.document_version_id,
            ranked.c.physical_index_id,
            ranked.c.search_index_id,
        )
        .where(ranked.c.rn == 1)
        .order_by(ranked.c.served_at)
        .limit(free)
    ).all()
    grouped: dict[UUID, list[NewItem]] = defaultdict(list)
    for row in rows:
        grouped[row.search_index_id].append(
            NewItem(
                ItemOperation.RERENDER,
                physical_index_id=row.physical_index_id,
                document_id=row.document_id,
                document_version_id=row.document_version_id,
                chunk_set_id=row.id,
            )
        )
    return _create_runs(connection, grouped)
