"""Sweep duty 7, the repoint (`14-ingestion.md` sections 8.7 to 8.10 and 5.3 duty 7; ADRs 0041,
0045 and 0052).

A logical index in `rebuilding` repoints to its building physical index once that index is
provisioned and serves every live document's current version. The duty counts the condition
without a lock, and only at zero runs one transaction that takes the logical index row's update
lock, counts once more and does nothing when the condition has stopped holding (a Flip into the
active index queued a newer version meanwhile). The repoint is the single pointer update: the
building index becomes `active`, the old one `retiring` with a `delete_after` 10 minutes ahead,
queued `ingest` items of the old index move to the new one or are cancelled where another item
covers their version there, and the rebuild run gets the old index's delete item, which the claim
starts no sooner than its `not_before`. The old index's served rows stay until that item runs, so
readers holding a cached index record still filter correctly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final
from uuid import UUID, uuid4

from sqlalchemy import (
    BindParameter,
    Connection,
    bindparam,
    exists,
    func,
    insert,
    literal_column,
    select,
    update,
)

from docchat_core.ingestion.models import ItemOperation, RunPriority
from docchat_core.sql.tables.ingestion import (
    documents,
    ingestion_run_items,
    physical_indexes,
    search_indexes,
    served_chunk_sets,
)
from docchat_core.sql.tables.schema import BIT_FALSE, SYSUTCDATETIME
from docchat_ingestion.activities.support import catalogue_message, dateadd, literal, literals

DELETE_DELAY_MINUTES: Final = 10
"""Ten times the 60-second index-record cache of chat, so in-flight queries finish."""
TARGET_REPOINTED: Final = "target_repointed"

_items = ingestion_run_items


@dataclass(frozen=True, slots=True)
class RebuildingIndex:
    """A logical index in `rebuilding` whose building physical index is provisioned."""

    index_id: UUID
    building_id: UUID
    building_generation: int
    rebuild_run_id: UUID | None


@dataclass(frozen=True, slots=True)
class Repointed:
    index_id: UUID
    old_physical_index_id: UUID | None
    new_physical_index_id: UUID
    generation: int


def rebuilding_indexes(connection: Connection) -> list[RebuildingIndex]:
    rows = connection.execute(
        select(
            search_indexes.c.id,
            physical_indexes.c.id.label("building_id"),
            physical_indexes.c.generation,
            physical_indexes.c.build_run_id,
        )
        .select_from(
            search_indexes.join(
                physical_indexes,
                (physical_indexes.c.search_index_id == search_indexes.c.id)
                & (physical_indexes.c.state == literal("building")),
            )
        )
        .where(
            search_indexes.c.status == literal("rebuilding"),
            physical_indexes.c.provisioned_at.is_not(None),
        )
        .order_by(search_indexes.c.id)
    ).all()
    return [
        RebuildingIndex(row.id, row.building_id, row.generation, row.build_run_id) for row in rows
    ]


def missing_documents(connection: Connection, index_id: UUID, building_id: UUID) -> int:
    """The repoint condition's count: live documents whose current version the building index
    doesn't serve (section 8.9)."""
    return int(
        connection.execute(
            select(func.count())
            .select_from(documents)
            .where(
                documents.c.search_index_id == index_id,
                documents.c.is_deleted == BIT_FALSE,
                documents.c.current_version_id.is_not(None),
                ~exists().where(
                    served_chunk_sets.c.document_id == documents.c.id,
                    served_chunk_sets.c.physical_index_id == building_id,
                    served_chunk_sets.c.document_version_id == documents.c.current_version_id,
                ),
            )
        ).scalar_one()
    )


def repoint(connection: Connection, index: RebuildingIndex) -> Repointed | None:
    """The repoint transaction; `None` when the condition stopped holding under the lock."""
    old: UUID | None = connection.execute(
        select(search_indexes.c.active_physical_index_id)
        .where(search_indexes.c.id == index.index_id)
        .with_hint(search_indexes, "WITH (UPDLOCK, ROWLOCK)", "mssql")
    ).scalar_one()
    if missing_documents(connection, index.index_id, index.building_id) != 0:
        return None
    new = index.building_id
    pointer = (
        search_indexes.c.active_physical_index_id.is_(None)
        if old is None
        else search_indexes.c.active_physical_index_id == old
    )
    connection.execute(
        update(search_indexes)
        .where(search_indexes.c.id == index.index_id, pointer)
        .values(active_physical_index_id=new)
    )
    # The old index leaves `active` before the new one enters it (section 8.9):
    # `UX_physical_indexes_one_active` is checked per statement, not at commit.
    if old is not None:
        connection.execute(
            update(physical_indexes)
            .where(physical_indexes.c.id == old, physical_indexes.c.state == literal("active"))
            .values(
                state=literal("retiring"),
                retiring_at=SYSUTCDATETIME,
                delete_after=dateadd("minute", DELETE_DELAY_MINUTES),
            )
        )
    connection.execute(
        update(physical_indexes)
        .where(physical_indexes.c.id == new, physical_indexes.c.state == literal("building"))
        .values(state=literal("active"), activated_at=SYSUTCDATETIME)
    )
    if old is not None:
        covering = _items.alias("c")
        connection.execute(
            update(_items)
            .where(
                _items.c.physical_index_id == old,
                _items.c.operation == literal(ItemOperation.INGEST),
                _items.c.state == literal("queued"),
                exists().where(
                    covering.c.document_version_id == _items.c.document_version_id,
                    covering.c.physical_index_id == new,
                    covering.c.state.in_(literals(("queued", "running", "succeeded"))),
                ),
            )
            .values(
                state=literal("cancelled"),
                outcome_code=TARGET_REPOINTED,
                outcome_message=catalogue_message(TARGET_REPOINTED),
                completed_at=SYSUTCDATETIME,
            )
        )
        connection.execute(
            update(_items)
            .where(
                _items.c.physical_index_id == old,
                _items.c.operation == literal(ItemOperation.INGEST),
                _items.c.state == literal("queued"),
            )
            .values(physical_index_id=new)
        )
        if index.rebuild_run_id is not None:
            connection.execute(
                insert(_items).from_select(
                    [
                        "id",
                        "run_id",
                        "operation",
                        "search_index_id",
                        "physical_index_id",
                        "priority",
                        "not_before",
                    ],
                    select(
                        _bound(uuid4()),
                        _bound(index.rebuild_run_id),
                        literal(ItemOperation.DELETE_PHYSICAL_INDEX),
                        _bound(index.index_id),
                        _bound(old),
                        literal_column(str(int(RunPriority.BULK))),
                        physical_indexes.c.delete_after,
                    ).where(physical_indexes.c.id == old),
                )
            )
    connection.execute(
        update(physical_indexes).where(physical_indexes.c.id == new).values(build_run_id=None)
    )
    return Repointed(index.index_id, old, new, index.building_generation)


def _bound(value: UUID) -> BindParameter[UUID]:
    return bindparam(None, value, type_=_items.c.id.type)
