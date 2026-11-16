"""`flip_chunk_set`, the Flip step (`14-ingestion.md` section 6.9; ADRs 0040, 0041 and 0045).

One transaction under READ COMMITTED with read-committed snapshot on, in which every guard is a
locking write or an `UPDLOCK` read, so each sees the latest committed row:

1. an earlier attempt already committed this Flip (its served row exists): `flipped`, with
   empty clean-up lists that sweep duty 6 finishes;
2. a target that stopped building after Validate (repointed or abandoned): the build is retired
   and the item re-targeted to the active index and requeued, or skipped when another item
   covers the version there (`target_repointed`) or there is no active index (`target_retired`);
3. into the active index, the version becomes the document's current one unless the document
   was deleted or a newer commit exists (the guard serialises with a delete through the row's
   update lock); into the building index, or for the current version, only `updated_at` moves;
4. zero rows from the applicable guard retire the build and supersede the version: `skipped`
   with `document_deleted` or `superseded_by_newer`; one row serves the build, retires the sets
   it replaces and, during a rebuild, queues the version for the building index in the rebuild
   run (`buildingItemQueued`).

From the commit on, readers of the physical index see only the new chunk set.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal
from uuid import UUID

from opentelemetry.trace import Span
from pydantic import Field
from sqlalchemy import (
    BindParameter,
    Connection,
    Exists,
    Row,
    and_,
    bindparam,
    exists,
    func,
    insert,
    literal_column,
    or_,
    select,
    update,
)
from sqlalchemy.dialects.mssql import UNIQUEIDENTIFIER

from docchat_core.ingestion.models import ActivityResult, ItemStage
from docchat_core.sql.tables.ingestion import (
    chunk_sets,
    document_versions,
    documents,
    ingestion_run_items,
    physical_indexes,
    search_indexes,
    served_chunk_sets,
)
from docchat_core.sql.tables.schema import SYSUTCDATETIME
from docchat_ingestion.activities.documents import (
    DEADLINES,
    ActivityOutput,
    BuildPlan,
    DocumentServices,
    PlanInput,
    ServicesProvider,
    blueprint,
    document_services,
    fence,
    item_ids,
    run_document_activity,
)
from docchat_ingestion.activities.support import literal, literals

NAME: Final = "flip_chunk_set"
DEADLINE_SECONDS: Final = DEADLINES[NAME]
LOCK_ROW: Final = "WITH (UPDLOCK, ROWLOCK)"
UPDLOCK: Final = "WITH (UPDLOCK)"
BULK_PRIORITY: Final = 1

_items = ingestion_run_items
_versions = document_versions

type FlipOutcome = Literal["flipped", "skipped", "requeued"]
type FlipSkip = Literal[
    "document_deleted", "superseded_by_newer", "target_repointed", "target_retired"
]


class FlipInvariantError(Exception):
    """A row the transaction must change didn't change: it rolls back as `internal_error`."""

    def __init__(self) -> None:
        super().__init__("flip invariant broken")


class FlipResult(ActivityResult):
    """`{outcome, code, retiredChunkSetIds, supersededVersionId, buildingItemQueued}` with the
    `ActivityResult` fields."""

    outcome: FlipOutcome | None = None
    code: FlipSkip | None = None
    retired_chunk_set_ids: list[UUID] = Field(default_factory=list)
    superseded_version_id: UUID | None = None
    building_item_queued: bool = False


@dataclass(frozen=True, slots=True)
class Flip:
    outcome: FlipOutcome
    code: FlipSkip | None = None
    retired: tuple[UUID, ...] = ()
    superseded: UUID | None = None
    building_item_queued: bool = False

    def result(self) -> FlipResult:
        return FlipResult.success(
            outcome=self.outcome,
            code=self.code,
            retired_chunk_set_ids=list(self.retired),
            superseded_version_id=self.superseded,
            building_item_queued=self.building_item_queued,
        )


def retire_build(connection: Connection, chunk_set_id: UUID) -> None:
    connection.execute(
        update(chunk_sets)
        .where(
            chunk_sets.c.id == chunk_set_id, chunk_sets.c.state.in_(literals(("building", "ready")))
        )
        .values(state=literal("retired"), retired_at=SYSUTCDATETIME)
    )


def covered(connection: Connection, version_id: UUID, physical_id: UUID) -> bool:
    """An item of any run covers the version in that physical index."""
    return (
        connection.execute(
            select(literal("1"))
            .select_from(_items)
            .where(
                _items.c.document_version_id == version_id,
                _items.c.physical_index_id == physical_id,
                _items.c.state.in_(literals(("queued", "running", "succeeded"))),
            )
            .limit(1)
        ).first()
        is not None
    )


def _newer_version(
    document_id: UUID, version_id: UUID, committed_at: object, number: int
) -> Exists:
    newer = _versions.alias("n")
    return exists().where(
        newer.c.document_id == document_id,
        newer.c.id != version_id,
        newer.c.state.in_(literals(("pending", "ingesting", "ingested"))),
        or_(
            newer.c.committed_at > committed_at,
            and_(newer.c.committed_at == committed_at, newer.c.version_number > number),
        ),
    )


def requeue_or_skip(connection: Connection, item: PlanInput, active: UUID | None) -> Flip:
    """Step (2): the target stopped building after Validate."""
    plan = item.plan
    retire_build(connection, plan.chunk_set_id)
    retired = (plan.chunk_set_id,)
    if active is None:
        return Flip("skipped", "target_retired", retired)
    if covered(connection, plan.version_id, active):
        return Flip("skipped", "target_repointed", retired)
    changed = connection.execute(
        update(_items)
        .where(
            _items.c.id == item.item_id,
            _items.c.claim_count == item.claim_number,
            _items.c.state == literal("running"),
        )
        .values(
            physical_index_id=active,
            state=literal("queued"),
            lease_expires_at=None,
            not_before=SYSUTCDATETIME,
            stage=None,
        )
    ).rowcount
    if changed != 1:
        raise FlipInvariantError
    return Flip("requeued", None, retired)


def make_current(
    connection: Connection, plan: BuildPlan, committed_at: object, number: int
) -> tuple[bool, UUID | None]:
    """Guard (4a): the version becomes current unless the document was deleted or a newer commit
    exists; returns whether the row changed and the version it replaced."""
    rows: Sequence[Row[Any]] = connection.execute(
        update(documents)
        .where(
            documents.c.id == plan.document_id,
            documents.c.is_deleted == literal_column("0"),
            or_(
                documents.c.current_version_id.is_(None),
                documents.c.current_version_id != plan.version_id,
            ),
            ~_newer_version(plan.document_id, plan.version_id, committed_at, number),
        )
        .values(current_version_id=plan.version_id, updated_at=SYSUTCDATETIME)
        .returning(
            literal_column("deleted.current_version_id", type_=documents.c.current_version_id.type)
        )
    ).all()
    if not rows:
        return False, None
    previous: UUID | None = rows[0][0]
    return True, previous


def touch_current(connection: Connection, plan: BuildPlan) -> bool:
    """Guard (4b): the current version re-ingested in place or into the building index."""
    changed = connection.execute(
        update(documents)
        .where(
            documents.c.id == plan.document_id,
            documents.c.is_deleted == literal_column("0"),
            documents.c.current_version_id == plan.version_id,
        )
        .values(updated_at=SYSUTCDATETIME)
    ).rowcount
    return bool(changed == 1)


def document_is_deleted(connection: Connection, document_id: UUID) -> bool:
    deleted = connection.execute(
        select(documents.c.is_deleted).where(documents.c.id == document_id)
    ).scalar_one()
    return bool(deleted)


def serve(connection: Connection, plan: BuildPlan) -> tuple[UUID, ...]:
    """The one-row path's serving writes: retire the sets served in this index, point the served
    row at the build and mark the build `served`."""
    retired = connection.execute(
        update(chunk_sets)
        .where(
            chunk_sets.c.id == served_chunk_sets.c.chunk_set_id,
            served_chunk_sets.c.document_id == plan.document_id,
            served_chunk_sets.c.physical_index_id == plan.physical_index.id,
        )
        .values(state=literal("retired"), retired_at=SYSUTCDATETIME)
        .returning(chunk_sets.c.id)
    ).all()
    pointed = connection.execute(
        update(served_chunk_sets)
        .where(
            served_chunk_sets.c.document_id == plan.document_id,
            served_chunk_sets.c.physical_index_id == plan.physical_index.id,
        )
        .values(
            chunk_set_id=plan.chunk_set_id,
            document_version_id=plan.version_id,
            served_at=SYSUTCDATETIME,
        )
    ).rowcount
    if pointed == 0:
        connection.execute(
            insert(served_chunk_sets).values(
                document_id=plan.document_id,
                physical_index_id=plan.physical_index.id,
                chunk_set_id=plan.chunk_set_id,
                document_version_id=plan.version_id,
            )
        )
    served = connection.execute(
        update(chunk_sets)
        .where(chunk_sets.c.id == plan.chunk_set_id, chunk_sets.c.state == literal("ready"))
        .values(state=literal("served"), served_at=SYSUTCDATETIME)
    ).rowcount
    if served != 1:
        raise FlipInvariantError
    return tuple(row[0] for row in retired)


def queue_for_building(connection: Connection, plan: BuildPlan) -> bool:
    """A case-(4a) Flip during a rebuild adds an `ingest` item for this version into the building
    index to its rebuild run, unless an item already covers it (section 8.8)."""
    building = connection.execute(
        select(physical_indexes.c.id, physical_indexes.c.build_run_id).where(
            physical_indexes.c.search_index_id == plan.index_id,
            physical_indexes.c.state == literal("building"),
        )
    ).first()
    if building is None or building.build_run_id is None:
        return False
    building_id: UUID = building.id
    run_id: UUID = building.build_run_id
    covering = exists().where(
        _items.c.document_version_id == plan.version_id,
        _items.c.physical_index_id == building_id,
        _items.c.state.in_(literals(("queued", "running", "succeeded"))),
    )
    in_run = exists().where(
        _items.c.run_id == run_id, _items.c.document_version_id == plan.version_id
    )
    added = connection.execute(
        insert(_items).from_select(
            [
                "id",
                "run_id",
                "operation",
                "search_index_id",
                "physical_index_id",
                "document_id",
                "document_version_id",
                "priority",
            ],
            select(
                func.newid(),
                _bound(run_id),
                literal("ingest"),
                _bound(plan.index_id),
                _bound(building_id),
                _bound(plan.document_id),
                _bound(plan.version_id),
                literal_column(str(BULK_PRIORITY)),
            ).where(~covering, ~in_run),
        )
    ).rowcount
    return bool(added == 1)


def _bound(value: UUID) -> BindParameter[UUID]:
    return bindparam(None, value, type_=UNIQUEIDENTIFIER)


def decide(connection: Connection, item: PlanInput) -> Flip:
    """The whole Flip transaction (section 6.9)."""
    plan = item.plan
    physical = plan.physical_index.id
    active: UUID | None = connection.execute(
        select(search_indexes.c.active_physical_index_id)
        .with_hint(search_indexes, LOCK_ROW, "mssql")
        .where(search_indexes.c.id == plan.index_id)
    ).scalar_one()
    fence(connection, item, ItemStage.FLIP, DEADLINE_SECONDS)
    already = connection.execute(
        select(literal("1"))
        .select_from(served_chunk_sets)
        .where(
            served_chunk_sets.c.document_id == plan.document_id,
            served_chunk_sets.c.physical_index_id == physical,
            served_chunk_sets.c.chunk_set_id == plan.chunk_set_id,
        )
    ).first()
    if already is not None:
        return Flip("flipped")
    target_state = connection.execute(
        select(physical_indexes.c.state)
        .with_hint(physical_indexes, UPDLOCK, "mssql")
        .where(physical_indexes.c.id == physical)
    ).scalar_one()
    if physical != active and target_state != "building":
        return requeue_or_skip(connection, item, active)
    version = connection.execute(
        select(_versions.c.committed_at, _versions.c.version_number)
        .with_hint(_versions, UPDLOCK, "mssql")
        .where(_versions.c.id == plan.version_id)
    ).one()
    into_active = physical == active
    changed, previous = False, None
    if into_active:
        changed, previous = make_current(
            connection, plan, version.committed_at, version.version_number
        )
    case_4a = changed
    if not changed:
        changed = touch_current(connection, plan)
    if not changed:
        retire_build(connection, plan.chunk_set_id)
        connection.execute(
            update(_versions)
            .where(_versions.c.id == plan.version_id, _versions.c.state == literal("ingesting"))
            .values(state=literal("superseded"), superseded_at=SYSUTCDATETIME)
        )
        code: FlipSkip = (
            "document_deleted"
            if document_is_deleted(connection, plan.document_id)
            else "superseded_by_newer"
        )
        return Flip("skipped", code, (plan.chunk_set_id,))
    if case_4a and previous is not None:
        connection.execute(
            update(_versions)
            .where(_versions.c.id == previous, _versions.c.state == literal("ingested"))
            .values(state=literal("superseded"), superseded_at=SYSUTCDATETIME)
        )
    ingested = connection.execute(
        update(_versions)
        .where(
            _versions.c.id == plan.version_id,
            _versions.c.state.in_(literals(("ingesting", "ingested"))),
        )
        .values(state=literal("ingested"), ingested_at=SYSUTCDATETIME)
    ).rowcount
    if ingested != 1:
        raise FlipInvariantError
    retired = serve(connection, plan)
    queued = queue_for_building(connection, plan) if case_4a else False
    return Flip(
        "flipped",
        None,
        retired,
        superseded=previous if case_4a else None,
        building_item_queued=queued,
    )


async def flip(services: DocumentServices, item: PlanInput, span: Span) -> FlipResult:
    del span
    decided = await services.sql.run(
        lambda connection: decide(connection, item), name="flip_chunk_set", retry="transient"
    )
    return decided.result()


def _failure(code: str, retry_after: int | None) -> FlipResult:
    return FlipResult.failure(code, retry_after_seconds=retry_after)


async def run(payload: object, provider: ServicesProvider = document_services) -> ActivityOutput:
    return await run_document_activity(
        NAME,
        payload,
        PlanInput,
        flip,
        stage=ItemStage.FLIP,
        provider=provider,
        ids=item_ids,
        failure=_failure,
    )


async def flip_chunk_set(payload: object) -> ActivityOutput:
    return await run(payload)


bp: Final = blueprint(flip_chunk_set)
