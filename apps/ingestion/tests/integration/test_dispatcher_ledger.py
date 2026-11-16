"""The dispatcher's SQL against the SQL Server container (T-149 lease expiry, T-150 the claim,
T-161 the sweep, T-178 the repoint, T-200 canary retention, and the run-retention half of T-199;
`14-ingestion.md` sections 4.8, 4.10, 5.3, 5.6, 6.13 and 8.9).

Each unit of work runs as `docchat_ingestion`, the role that runs it in production, on a
synchronous connection inside one transaction, as the data-access layer runs it; rows are arranged
and read back as `sa`.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import Engine, Table, insert, select, update

from docchat_core.ingestion.models import ItemStage
from docchat_core.sql.tables.access import projects
from docchat_core.sql.tables.ingestion import (
    chunk_sets,
    document_versions,
    documents,
    ingestion_run_items,
    ingestion_runs,
    physical_indexes,
    scan_canaries,
    search_indexes,
    served_chunk_sets,
)
from docchat_ingestion.activities import claim_ingestion_items, finalisation, repoint, sweep_duties
from docchat_ingestion.activities.support import fence_lease
from docchat_ingestion.starters import (
    delete_old_canaries,
    delete_old_items,
    delete_old_runs,
    expired_open_canaries,
)
from docchat_testing.builders import project_row
from docchat_testing.builders.ingestion import (
    chunk_set_row,
    document_row,
    document_version_row,
    ingestion_run_item_row,
    ingestion_run_row,
    physical_index_row,
    scan_canary_row,
    search_index_row,
    served_chunk_set_row,
)
from docchat_testing.builders.rows import sql_now
from docchat_testing.fixtures.sql import SqlTestDatabase

pytestmark = pytest.mark.integration


@dataclass(frozen=True)
class Engines:
    admin: Engine
    ingestion: Engine


@pytest.fixture
def engines(sql_database: SqlTestDatabase) -> Iterator[Engines]:
    created = Engines(
        admin=sql_database.engine(), ingestion=sql_database.engine("docchat_ingestion")
    )
    yield created
    created.admin.dispose()
    created.ingestion.dispose()


def put(engine: Engine, *rows: tuple[Table, dict[str, Any]]) -> None:
    with engine.begin() as connection:
        for table, row in rows:
            connection.execute(insert(table), row)


def unit(engine: Engine, work: Any) -> Any:
    with engine.begin() as connection:
        return work(connection)


def rows_of(engine: Engine, statement: Any) -> list[Any]:
    with engine.connect() as connection:
        return list(connection.execute(statement).all())


@dataclass(frozen=True)
class Index:
    project_id: UUID
    index_id: UUID
    active_id: UUID


def ready_index(engines: Engines) -> Index:
    project = project_row()
    index = search_index_row(status="ready")
    physical = physical_index_row(index["id"], state="active", provisioned_at=sql_now())
    put(engines.admin, (projects, project), (search_indexes, index), (physical_indexes, physical))
    with engines.admin.begin() as connection:
        connection.execute(
            update(search_indexes)
            .where(search_indexes.c.id == index["id"])
            .values(active_physical_index_id=physical["id"])
        )
    return Index(project["id"], index["id"], physical["id"])


def pending_version(engines: Engines, index: Index, **overrides: Any) -> tuple[UUID, UUID]:
    document = document_row(index.index_id)
    values: dict[str, Any] = {"state": "pending", "committed_at": sql_now()}
    values.update(overrides)
    version = document_version_row(document["id"], index.index_id, index.project_id, **values)
    put(engines.admin, (documents, document), (document_versions, version))
    return document["id"], version["id"]


def queued_item(
    engines: Engines,
    index: Index,
    document_id: UUID,
    version_id: UUID,
    *,
    priority: int = 0,
    **values: Any,
) -> UUID:
    run = ingestion_run_row(index.index_id, priority=priority)
    item = ingestion_run_item_row(
        run["id"],
        index.index_id,
        document_id=document_id,
        document_version_id=version_id,
        physical_index_id=index.active_id,
        priority=priority,
        **values,
    )
    put(engines.admin, (ingestion_runs, run), (ingestion_run_items, item))
    item_id: UUID = item["id"]
    return item_id


def test_t149_an_expired_lease_requeues_then_fails_the_item(engines: Engines) -> None:
    index = ready_index(engines)
    document_id, version_id = pending_version(engines, index)
    expired = sql_now() - timedelta(minutes=1)
    item = queued_item(
        engines,
        index,
        document_id,
        version_id,
        state="running",
        claim_count=1,
        lease_expires_at=expired,
    )

    leases = unit(engines.ingestion, lambda c: sweep_duties.expire_leases(c, 3))
    assert leases.requeued == (item,)
    assert (
        unit(engines.ingestion, lambda c: fence_lease(c, item, 1, ItemStage.EMBED, 1560)) is False
    )

    with engines.admin.begin() as connection:
        connection.execute(
            update(ingestion_run_items)
            .where(ingestion_run_items.c.id == item)
            .values(state="running", claim_count=3, lease_expires_at=expired)
        )
    leases = unit(engines.ingestion, lambda c: sweep_duties.expire_leases(c, 3))

    assert [failed.id for failed in leases.failed] == [item]
    (row,) = rows_of(
        engines.admin, select(ingestion_run_items).where(ingestion_run_items.c.id == item)
    )
    assert (row.state, row.outcome_code) == ("failed", "processing_interrupted")
    (version,) = rows_of(
        engines.admin, select(document_versions.c.state).where(document_versions.c.id == version_id)
    )
    assert version.state == "failed"


def test_t150_the_claim_orders_gates_and_counts_slots(engines: Engines) -> None:
    index = ready_index(engines)
    bulk_doc, bulk_version = pending_version(engines, index)
    first_doc, first_version = pending_version(engines, index)
    _, second_version = pending_version(engines, index)
    scanned_doc, scanned_version = pending_version(
        engines, index, scan_required=True, scan_status="awaiting"
    )
    bulk = queued_item(engines, index, bulk_doc, bulk_version, priority=1)
    first = queued_item(engines, index, first_doc, first_version)
    same_document = queued_item(engines, index, first_doc, second_version)
    gated = queued_item(engines, index, scanned_doc, scanned_version)

    claimed, _ = unit(engines.ingestion, lambda c: claim_ingestion_items.claim(c, 2))

    assert [item.item_id for item in claimed] == [first, bulk]
    assert all(item.instance_id == f"docchat-item-{item.item_id}-1" for item in claimed)
    again, _ = unit(engines.ingestion, lambda c: claim_ingestion_items.claim(c, 2))
    assert again == []
    states = dict(
        rows_of(engines.admin, select(ingestion_run_items.c.id, ingestion_run_items.c.state))
    )
    assert states[same_document] == "queued"
    assert states[gated] == "queued"


def test_t150_a_purge_waits_for_every_item_of_its_index(engines: Engines) -> None:
    index = ready_index(engines)
    other = ready_index(engines)
    document_id, version_id = pending_version(engines, index)
    other_doc, other_version = pending_version(engines, other)
    queued_item(
        engines,
        index,
        document_id,
        version_id,
        state="running",
        claim_count=1,
        stage="render",
        lease_expires_at=sql_now() + timedelta(minutes=30),
    )
    run = ingestion_run_row(index.index_id, kind="index_delete", priority=1)
    purge = ingestion_run_item_row(run["id"], index.index_id, priority=1)
    put(engines.admin, (ingestion_runs, run), (ingestion_run_items, purge))
    other_item = queued_item(engines, other, other_doc, other_version, priority=1)

    claimed, _ = unit(engines.ingestion, lambda c: claim_ingestion_items.claim(c, 4))

    assert [item.item_id for item in claimed] == [other_item]


def test_t161_the_sweep_removes_stale_uploads_and_queues_orphans_and_leftovers(
    engines: Engines,
) -> None:
    index = ready_index(engines)
    stale_doc = document_row(index.index_id)
    stale = document_version_row(
        stale_doc["id"],
        index.index_id,
        index.project_id,
        reserved_at=sql_now() - timedelta(hours=25),
    )
    put(engines.admin, (documents, stale_doc), (document_versions, stale))
    _, orphan = pending_version(engines, index, committed_at=sql_now() - timedelta(minutes=20))
    leftover_doc, leftover = pending_version(engines, index)
    with engines.admin.begin() as connection:
        connection.execute(
            update(document_versions)
            .where(document_versions.c.id == leftover)
            .values(state="superseded", superseded_at=sql_now() - timedelta(minutes=20))
        )

    (upload,) = unit(engines.ingestion, lambda c: sweep_duties.stale_uploads(c, 24))
    assert unit(engines.ingestion, lambda c: sweep_duties.remove_stale_upload(c, upload)) is True
    assert unit(engines.ingestion, lambda c: sweep_duties.queue_orphaned_versions(c, 15))
    assert unit(engines.ingestion, lambda c: sweep_duties.queue_leftover_cleanup(c, 15))

    assert (
        rows_of(engines.admin, select(documents.c.id).where(documents.c.id == stale_doc["id"]))
        == []
    )
    items = rows_of(
        engines.admin,
        select(
            ingestion_run_items.c.operation,
            ingestion_run_items.c.document_version_id,
            ingestion_run_items.c.document_id,
        ),
    )
    assert ("ingest", orphan) in {(row.operation, row.document_version_id) for row in items}
    assert ("cleanup_document", leftover_doc) in {(row.operation, row.document_id) for row in items}


def test_t161_finalisation_sets_states_and_totals(engines: Engines) -> None:
    index = ready_index(engines)
    document_id, version_id = pending_version(engines, index)
    other_doc, other_version = pending_version(engines, index)
    run = ingestion_run_row(index.index_id, state="running")
    put(
        engines.admin,
        (ingestion_runs, run),
        (
            ingestion_run_items,
            ingestion_run_item_row(
                run["id"],
                index.index_id,
                document_id=document_id,
                document_version_id=version_id,
                physical_index_id=index.active_id,
                state="succeeded",
            ),
        ),
        (
            ingestion_run_items,
            ingestion_run_item_row(
                run["id"],
                index.index_id,
                document_id=other_doc,
                document_version_id=other_version,
                physical_index_id=index.active_id,
                state="failed",
                outcome_code="parse_failed",
            ),
        ),
    )

    runs = unit(engines.ingestion, finalisation.finalisable_runs)
    finished = [unit(engines.ingestion, lambda c, r=r: finalisation.finalise(c, r)) for r in runs]

    (done,) = [item for item in finished if item is not None and item.run_id == run["id"]]
    assert (done.state.value, done.total, done.succeeded, done.failed) == (
        "completed_with_errors",
        2,
        1,
        1,
    )


def test_t178_the_repoint_moves_the_pointer_and_queues_the_delete(engines: Engines) -> None:
    index = ready_index(engines)
    run = ingestion_run_row(index.index_id, kind="index_rebuild", priority=1, state="running")
    put(engines.admin, (ingestion_runs, run))
    building = physical_index_row(
        index.index_id,
        generation=2,
        state="building",
        provisioned_at=sql_now(),
        build_run_id=run["id"],
    )
    document_id, version_id = pending_version(engines, index)
    chunk_set = chunk_set_row(document_id, version_id, building["id"], state="served")
    put(engines.admin, (physical_indexes, building), (chunk_sets, chunk_set))
    with engines.admin.begin() as connection:
        connection.execute(
            update(documents)
            .where(documents.c.id == document_id)
            .values(current_version_id=version_id)
        )
        connection.execute(
            update(search_indexes)
            .where(search_indexes.c.id == index.index_id)
            .values(status="rebuilding")
        )
        connection.execute(
            insert(served_chunk_sets),
            served_chunk_set_row(document_id, building["id"], chunk_set["id"], version_id),
        )

    (rebuilding,) = unit(engines.ingestion, repoint.rebuilding_indexes)
    done = unit(engines.ingestion, lambda c: repoint.repoint(c, rebuilding))

    assert done is not None
    (pointer,) = rows_of(
        engines.admin,
        select(search_indexes.c.active_physical_index_id).where(
            search_indexes.c.id == index.index_id
        ),
    )
    assert pointer.active_physical_index_id == building["id"]
    deletes = rows_of(
        engines.admin,
        select(ingestion_run_items).where(
            ingestion_run_items.c.operation == "delete_physical_index"
        ),
    )
    assert [(row.physical_index_id, row.run_id) for row in deletes] == [
        (index.active_id, run["id"])
    ]
    assert deletes[0].not_before is not None


def test_t200_canary_retention_deletes_rows_older_than_90_days(engines: Engines) -> None:
    old = scan_canary_row(written_at=sql_now() - timedelta(days=91))
    young = scan_canary_row(written_at=sql_now() - timedelta(days=89))
    put(engines.admin, (scan_canaries, old), (scan_canaries, young))

    assert unit(engines.ingestion, expired_open_canaries) == [old["blob_path"]]
    while unit(engines.ingestion, delete_old_canaries):
        pass

    remaining = rows_of(engines.admin, select(scan_canaries.c.id))
    assert [row.id for row in remaining] == [young["id"]]


def test_t199_run_retention_deletes_finished_runs_past_180_days(engines: Engines) -> None:
    index = ready_index(engines)
    old = ingestion_run_row(
        index.index_id, state="completed", completed_at=sql_now() - timedelta(days=181)
    )
    recent = ingestion_run_row(
        index.index_id, state="completed", completed_at=sql_now() - timedelta(days=1)
    )
    put(engines.admin, (ingestion_runs, old), (ingestion_runs, recent))

    while unit(engines.ingestion, lambda c: delete_old_items(c, 180)):
        pass
    while unit(engines.ingestion, lambda c: delete_old_runs(c, 180)):
        pass

    ids = {row.id for row in rows_of(engines.admin, select(ingestion_runs.c.id))}
    assert old["id"] not in ids
    assert recent["id"] in ids
