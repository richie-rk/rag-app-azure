"""Run creation, the failure side effects and the logical index status against the SQL Server
container (T-179, T-198, and the SQL halves of T-140's `UX_run_items_run_version` and T-176;
`14-ingestion.md` sections 2.6, 6.11, 7, 8.3 to 8.7 and 8.11; `80-data-model.md` sections 2.16,
2.17, 2.22 and 8.4).

Each function runs as the role that calls it in production (`docchat_api` for the admin
operations, `docchat_config_operator` for `docchat-ops reindex start` and, as the narrowest of
its three callers, for the failure side effects) on a synchronous connection inside one
transaction, as the caller's unit of work runs it; rows are arranged and read back as `sa`.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import Connection, Engine, Table, insert, select, update
from sqlalchemy.exc import IntegrityError

from docchat_core.ingestion.methods import RECURSIVE_V1
from docchat_core.ingestion.models import (
    IndexStatus,
    IngestionRefusedError,
    ItemOperation,
    ReingestMode,
    SubmitOutcome,
    TraceContext,
)
from docchat_core.ingestion.runs import (
    FailureSideEffects,
    IndexSettings,
    NewItem,
    apply_failure_side_effects,
    compute_index_status,
    create_document_delete_run,
    create_document_reingest_run,
    create_index_create_run,
    create_index_delete_run,
    create_maintenance_run,
    create_reingest_run,
    create_upload_run,
)
from docchat_core.ingestion.settings import DEFAULT_EMBEDDING_MODELS, EmbeddingModelCatalog
from docchat_core.sql.tables.access import project_indexes, projects
from docchat_core.sql.tables.ingestion import (
    chunk_sets,
    document_versions,
    documents,
    ingestion_run_items,
    ingestion_runs,
    physical_indexes,
    search_indexes,
    served_chunk_sets,
)
from docchat_testing.builders import project_index_row, project_row
from docchat_testing.builders.ingestion import (
    chunk_set_row,
    document_row,
    document_version_row,
    ingestion_run_item_row,
    ingestion_run_row,
    physical_index_row,
    search_index_row,
    served_chunk_set_row,
)
from docchat_testing.builders.rows import compact_json, new_id, sql_now
from docchat_testing.fixtures.sql import SqlTestDatabase

pytestmark = pytest.mark.integration

TRACE = TraceContext("00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01")
CATALOG = EmbeddingModelCatalog.model_construct(openai_embedding_models=DEFAULT_EMBEDDING_MODELS)


@dataclass(frozen=True)
class Engines:
    admin: Engine
    api: Engine
    operator: Engine


@pytest.fixture
def engines(sql_database: SqlTestDatabase) -> Iterator[Engines]:
    created = Engines(
        admin=sql_database.engine(),
        api=sql_database.engine("docchat_api"),
        operator=sql_database.engine("docchat_config_operator"),
    )
    yield created
    for engine in (created.admin, created.api, created.operator):
        engine.dispose()


def put(engine: Engine, *rows: tuple[Table, dict[str, Any]]) -> None:
    with engine.begin() as connection:
        for table, row in rows:
            connection.execute(insert(table), row)


def scalar(engine: Engine, statement: Any) -> Any:
    with engine.connect() as connection:
        return connection.execute(statement).scalar_one()


@dataclass(frozen=True)
class Ledger:
    """A `ready` logical index of the recursive method with its active, provisioned generation
    1, mapped to an active project."""

    project_id: UUID
    index_id: UUID
    active_id: UUID


def ledger(engines: Engines, *, status: str = "ready", mapped: bool = False) -> Ledger:
    project = project_row()
    parameters = RECURSIVE_V1.default_parameters()
    index = search_index_row(
        method_key="recursive",
        method_version=1,
        method_parameters=compact_json(parameters.dumped()),
        method_parameter_hash=RECURSIVE_V1.parameter_hash(parameters),
        status=status,
    )
    physical = physical_index_row(index["id"], state="active", provisioned_at=sql_now())
    rows: list[tuple[Table, dict[str, Any]]] = [
        (projects, project),
        (search_indexes, index),
        (physical_indexes, physical),
    ]
    if mapped:
        rows.append(
            (project_indexes, project_index_row(project["id"], index["id"], is_default=True))
        )
    put(engines.admin, *rows)
    with engines.admin.begin() as connection:
        connection.execute(
            update(search_indexes)
            .where(search_indexes.c.id == index["id"])
            .values(active_physical_index_id=physical["id"])
        )
    return Ledger(project["id"], index["id"], physical["id"])


def live_document(
    engines: Engines, place: Ledger, *, served_with: bytes | None = None
) -> tuple[UUID, UUID]:
    """A live document whose version 1 is `ingested`, current and served in the active index
    by a chunk set of the recursive method with `served_with` as its hash (the index's own by
    default)."""
    document = document_row(place.index_id)
    version = document_version_row(
        document["id"], place.index_id, place.project_id, state="ingested", committed_at=sql_now()
    )
    parameters = RECURSIVE_V1.default_parameters()
    chunk_set = chunk_set_row(
        document["id"],
        version["id"],
        place.active_id,
        method_key="recursive",
        method_parameters=compact_json(parameters.dumped()),
        parameter_hash=served_with or RECURSIVE_V1.parameter_hash(parameters),
        state="served",
        chunk_count=3,
    )
    put(
        engines.admin,
        (documents, document),
        (document_versions, version),
        (chunk_sets, chunk_set),
        (
            served_chunk_sets,
            served_chunk_set_row(document["id"], place.active_id, chunk_set["id"], version["id"]),
        ),
    )
    with engines.admin.begin() as connection:
        connection.execute(
            update(documents)
            .where(documents.c.id == document["id"])
            .values(current_version_id=version["id"])
        )
    return document["id"], version["id"]


def row_version(engine: Engine, table: Table, row_id: UUID) -> bytes:
    return bytes(scalar(engine, select(table.c.row_version).where(table.c.id == row_id)))


def items_of(engine: Engine, run_id: UUID) -> list[Any]:
    with engine.connect() as connection:
        return list(
            connection.execute(
                select(ingestion_run_items)
                .where(ingestion_run_items.c.run_id == run_id)
                .order_by(ingestion_run_items.c.queue_seq)
            )
        )


# --- Upload ---------------------------------------------------------------------------------------


def test_an_upload_run_queues_pending_versions_once(engines: Engines) -> None:
    place = ledger(engines)
    document = document_row(place.index_id)
    pending = document_version_row(
        document["id"], place.index_id, place.project_id, state="pending", committed_at=sql_now()
    )
    uploading = document_version_row(
        document["id"], place.index_id, place.project_id, version_number=2
    )
    put(
        engines.admin,
        (documents, document),
        (document_versions, pending),
        (document_versions, uploading),
    )
    oid = new_id()
    with engines.api.begin() as connection:
        first = create_upload_run(
            connection,
            index_id=place.index_id,
            version_ids=[pending["id"], uploading["id"], new_id()],
            requested_by=oid,
            trace=TRACE,
        )
    assert [result.outcome for result in first.results] == [
        SubmitOutcome.QUEUED,
        SubmitOutcome.NOT_PENDING,
        SubmitOutcome.NOT_FOUND,
    ]
    assert first.run_id is not None
    (item,) = items_of(engines.admin, first.run_id)
    assert (item.operation, item.physical_index_id, item.state) == (
        "ingest",
        place.active_id,
        "queued",
    )
    with engines.admin.connect() as connection:
        run = connection.execute(
            select(ingestion_runs).where(ingestion_runs.c.id == first.run_id)
        ).one()
    assert (run.kind, run.priority, run.state, run.requested_by) == ("upload", 0, "queued", oid)
    assert run.traceparent == TRACE.traceparent
    with engines.api.begin() as connection:
        second = create_upload_run(
            connection,
            index_id=place.index_id,
            version_ids=[pending["id"]],
            requested_by=oid,
            trace=TRACE,
        )
    assert second.run_id is None
    assert second.results[0].outcome is SubmitOutcome.ALREADY_QUEUED


def test_t140_one_item_per_version_per_run(engines: Engines) -> None:
    place = ledger(engines)
    document = document_row(place.index_id)
    version = document_version_row(
        document["id"], place.index_id, place.project_id, state="pending", committed_at=sql_now()
    )
    run = ingestion_run_row(place.index_id)
    item = ingestion_run_item_row(
        run["id"],
        place.index_id,
        document_id=document["id"],
        document_version_id=version["id"],
        physical_index_id=place.active_id,
    )
    put(
        engines.admin,
        (documents, document),
        (document_versions, version),
        (ingestion_runs, run),
        (ingestion_run_items, item),
    )
    duplicate = {**item, "id": new_id()}
    with pytest.raises(IntegrityError), engines.admin.begin() as connection:
        connection.execute(insert(ingestion_run_items), duplicate)


# --- Documents ------------------------------------------------------------------------------------


def test_t176_a_document_delete_hides_the_document_and_queues_its_clean_up(
    engines: Engines,
) -> None:
    place = ledger(engines)
    document_id, version_id = live_document(engines, place)
    uploading = document_version_row(
        document_id, place.index_id, place.project_id, version_number=2
    )
    run = ingestion_run_row(place.index_id)
    queued = ingestion_run_item_row(
        run["id"],
        place.index_id,
        document_id=document_id,
        document_version_id=version_id,
        physical_index_id=place.active_id,
    )
    put(
        engines.admin,
        (document_versions, uploading),
        (ingestion_runs, run),
        (ingestion_run_items, queued),
    )
    oid = new_id()
    with engines.api.begin() as connection:
        result = create_document_delete_run(
            connection,
            index_id=place.index_id,
            document_id=document_id,
            row_version=row_version(engines.admin, documents, document_id),
            requested_by=oid,
            trace=TRACE,
        )
    assert (result.superseded_version_count, result.cancelled_item_count) == (1, 1)
    with engines.admin.connect() as connection:
        document = connection.execute(select(documents).where(documents.c.id == document_id)).one()
        states = dict(
            connection.execute(
                select(document_versions.c.id, document_versions.c.state).where(
                    document_versions.c.document_id == document_id
                )
            ).all()
        )
        served = connection.execute(
            select(served_chunk_sets).where(served_chunk_sets.c.document_id == document_id)
        ).all()
        chunk_states = (
            connection.execute(
                select(chunk_sets.c.state).where(chunk_sets.c.document_id == document_id)
            )
            .scalars()
            .all()
        )
        cancelled = connection.execute(
            select(ingestion_run_items).where(ingestion_run_items.c.id == queued["id"])
        ).one()
    assert (document.is_deleted, document.deleted_by, document.current_version_id) == (
        True,
        oid,
        None,
    )
    assert states == {version_id: "superseded", uploading["id"]: "failed"}
    assert served == []
    assert chunk_states == ["retired"]
    assert (cancelled.state, cancelled.outcome_code) == ("cancelled", "document_deleted")
    (clean_up,) = items_of(engines.admin, result.run_id)
    assert (clean_up.operation, clean_up.document_id) == ("cleanup_document", document_id)


def test_a_stale_document_delete_changes_nothing(engines: Engines) -> None:
    place = ledger(engines)
    document_id, _ = live_document(engines, place)
    with pytest.raises(IngestionRefusedError) as refused, engines.api.begin() as connection:
        create_document_delete_run(
            connection,
            index_id=place.index_id,
            document_id=document_id,
            row_version=b"\x00" * 8,
            requested_by=new_id(),
            trace=None,
        )
    assert refused.value.code == "precondition_failed"
    assert (
        scalar(engines.admin, select(documents.c.is_deleted).where(documents.c.id == document_id))
        is False
    )


def test_a_document_reingest_reuses_an_open_item(engines: Engines) -> None:
    place = ledger(engines)
    document_id, version_id = live_document(engines, place)

    def reingest(connection: Connection) -> Any:
        return create_document_reingest_run(
            connection,
            index_id=place.index_id,
            document_id=document_id,
            row_version=row_version(engines.admin, documents, document_id),
            requested_by=new_id(),
            trace=TRACE,
        )

    with engines.api.begin() as connection:
        first = reingest(connection)
    assert (first.created, first.item_count) == (True, 1)
    (item,) = items_of(engines.admin, first.run_id)
    assert (item.document_version_id, item.physical_index_id) == (version_id, place.active_id)
    with engines.api.begin() as connection:
        second = reingest(connection)
    assert (second.run_id, second.created, second.item_count) == (first.run_id, False, 0)


# --- Index creation and capacity (T-198) ----------------------------------------------------------


def settings() -> IndexSettings:
    return IndexSettings(
        method=RECURSIVE_V1,
        parameters=RECURSIVE_V1.default_parameters(),
        embedding=DEFAULT_EMBEDDING_MODELS[0],
        dimensions=1024,
        analyzer="standard.lucene",
    )


def test_an_index_create_writes_its_three_rows_and_one_item(engines: Engines) -> None:
    creator = new_id()
    with engines.api.begin() as connection:
        result = create_index_create_run(
            connection,
            display_name="Policies",
            description=None,
            settings=settings(),
            max_indexes=15,
            created_by=creator,
            trace=TRACE,
        )
    with engines.admin.connect() as connection:
        index = connection.execute(
            select(search_indexes).where(search_indexes.c.id == result.index_id)
        ).one()
        physical = connection.execute(
            select(physical_indexes).where(physical_indexes.c.id == result.physical_index_id)
        ).one()
    assert (index.status, index.active_physical_index_id) == ("provisioning", None)
    assert (physical.state, physical.generation, physical.build_run_id) == (
        "building",
        1,
        result.run_id,
    )
    (item,) = items_of(engines.admin, result.run_id)
    assert item.operation == ItemOperation.CREATE_PHYSICAL_INDEX


def _fill(engines: Engines, count: int) -> None:
    for _ in range(count):
        index = search_index_row()
        put(
            engines.admin,
            (search_indexes, index),
            (physical_indexes, physical_index_row(index["id"])),
        )


def test_t198_creation_refuses_to_take_the_last_free_slot(engines: Engines) -> None:
    _fill(engines, 3)
    with pytest.raises(IngestionRefusedError) as refused, engines.api.begin() as connection:
        create_index_create_run(
            connection,
            display_name="One too many",
            description=None,
            settings=settings(),
            max_indexes=4,
            created_by=new_id(),
            trace=None,
        )
    assert refused.value.code == "search_index_capacity"


def test_t198_a_rebuild_may_take_the_free_slot_but_no_more(engines: Engines) -> None:
    place = ledger(engines)
    with engines.admin.begin() as connection:
        connection.execute(
            update(search_indexes)
            .where(search_indexes.c.id == place.index_id)
            .values(language_analyzer="en.microsoft")
        )
    _fill(engines, 2)

    def rebuild(max_indexes: int) -> Any:
        with engines.operator.begin() as connection:
            return create_reingest_run(
                connection,
                index_id=place.index_id,
                row_version=row_version(engines.admin, search_indexes, place.index_id),
                requested_by=new_id(),
                trace=None,
                max_indexes=max_indexes,
                catalog=CATALOG,
            )

    with pytest.raises(IngestionRefusedError) as refused:
        rebuild(3)
    assert refused.value.code == "search_index_capacity"
    result = rebuild(4)
    assert result.mode is ReingestMode.NEW_PHYSICAL_INDEX


# --- Re-ingest and rebuild ------------------------------------------------------------------------


def test_a_reingest_in_place_queues_each_live_current_version(engines: Engines) -> None:
    place = ledger(engines, status="reingest_required")
    live_document(engines, place)
    live_document(engines, place)
    with engines.operator.begin() as connection:
        result = create_reingest_run(
            connection,
            index_id=place.index_id,
            row_version=row_version(engines.admin, search_indexes, place.index_id),
            requested_by=new_id(),
            trace=None,
            max_indexes=15,
            catalog=CATALOG,
        )
    assert (result.mode, result.item_count, result.status) == (
        ReingestMode.SAME_INDEX,
        2,
        IndexStatus.REINGESTING,
    )
    assert (
        scalar(
            engines.admin,
            select(search_indexes.c.status).where(search_indexes.c.id == place.index_id),
        )
        == "reingesting"
    )
    assert {item.priority for item in items_of(engines.admin, result.run_id)} == {1}


def test_a_failed_index_rebuild_retires_the_leftover_and_moves_its_items(engines: Engines) -> None:
    project = project_row()
    index = search_index_row(status="failed")
    leftover = physical_index_row(index["id"])
    document = document_row(index["id"])
    version = document_version_row(
        document["id"], index["id"], project["id"], state="pending", committed_at=sql_now()
    )
    upload = ingestion_run_row(index["id"])
    waiting = ingestion_run_item_row(
        upload["id"],
        index["id"],
        document_id=document["id"],
        document_version_id=version["id"],
        physical_index_id=leftover["id"],
    )
    put(
        engines.admin,
        (projects, project),
        (search_indexes, index),
        (physical_indexes, leftover),
        (documents, document),
        (document_versions, version),
        (ingestion_runs, upload),
        (ingestion_run_items, waiting),
    )
    with engines.api.begin() as connection:
        result = create_reingest_run(
            connection,
            index_id=index["id"],
            row_version=row_version(engines.admin, search_indexes, index["id"]),
            requested_by=new_id(),
            trace=TRACE,
            max_indexes=15,
            catalog=CATALOG,
        )
    with engines.admin.connect() as connection:
        retired = connection.execute(
            select(physical_indexes).where(physical_indexes.c.id == leftover["id"])
        ).one()
        moved = connection.execute(
            select(ingestion_run_items.c.physical_index_id).where(
                ingestion_run_items.c.id == waiting["id"]
            )
        ).scalar_one()
        new = connection.execute(
            select(physical_indexes).where(physical_indexes.c.id == result.physical_index_id)
        ).one()
    assert (retired.state, retired.build_run_id) == ("retiring", None)
    assert retired.delete_after is not None
    assert moved == result.physical_index_id
    assert (new.state, new.generation, new.build_run_id) == ("building", 2, result.run_id)
    assert [item.operation for item in items_of(engines.admin, result.run_id)] == [
        "create_physical_index"
    ]


# --- Index delete ---------------------------------------------------------------------------------


def test_an_index_delete_refuses_a_mapped_index(engines: Engines) -> None:
    place = ledger(engines, mapped=True)
    with pytest.raises(IngestionRefusedError) as refused, engines.api.begin() as connection:
        create_index_delete_run(
            connection,
            index_id=place.index_id,
            row_version=row_version(engines.admin, search_indexes, place.index_id),
            requested_by=new_id(),
            trace=None,
        )
    assert refused.value.code == "index_mapped"


def test_an_index_delete_removes_every_document_and_queues_every_deletion(
    engines: Engines,
) -> None:
    place = ledger(engines)
    document_id, _ = live_document(engines, place)
    with engines.api.begin() as connection:
        result = create_index_delete_run(
            connection,
            index_id=place.index_id,
            row_version=row_version(engines.admin, search_indexes, place.index_id),
            requested_by=new_id(),
            trace=TRACE,
        )
    assert (result.physical_index_count, result.document_count) == (1, 1)
    assert (
        scalar(
            engines.admin,
            select(search_indexes.c.status).where(search_indexes.c.id == place.index_id),
        )
        == "deleting"
    )
    assert scalar(
        engines.admin, select(documents.c.is_deleted).where(documents.c.id == document_id)
    )
    assert [(item.operation, item.priority) for item in items_of(engines.admin, result.run_id)] == [
        ("delete_physical_index", 1),
        ("purge_index_blobs", 1),
    ]


# --- Maintenance and the status (T-179) -----------------------------------------------------------


def test_a_maintenance_run_has_no_requester(engines: Engines) -> None:
    place = ledger(engines)
    with engines.admin.begin() as connection:
        created = create_maintenance_run(
            connection,
            index_id=place.index_id,
            items=[NewItem(ItemOperation.DELETE_PHYSICAL_INDEX, physical_index_id=place.active_id)],
        )
    with engines.admin.connect() as connection:
        run = connection.execute(
            select(ingestion_runs).where(ingestion_runs.c.id == created.run_id)
        ).one()
    assert (run.kind, run.requested_by, run.traceparent) == ("maintenance", None, None)


def test_t179_the_status_follows_the_method_of_the_served_sets(engines: Engines) -> None:
    place = ledger(engines)
    live_document(engines, place)
    with engines.api.begin() as connection:
        assert compute_index_status(connection, place.index_id) is IndexStatus.READY
    changed = RECURSIVE_V1.validate_parameters({"targetTokens": 1024})
    with engines.admin.begin() as connection:
        connection.execute(
            update(search_indexes)
            .where(search_indexes.c.id == place.index_id)
            .values(
                method_parameters=compact_json(changed.dumped()),
                method_parameter_hash=RECURSIVE_V1.parameter_hash(changed),
            )
        )
    with engines.api.begin() as connection:
        assert compute_index_status(connection, place.index_id) is IndexStatus.REINGEST_REQUIRED
    with engines.admin.begin() as connection:
        connection.execute(
            update(chunk_sets)
            .where(chunk_sets.c.physical_index_id == place.active_id)
            .values(parameter_hash=RECURSIVE_V1.parameter_hash(changed))
        )
    with engines.api.begin() as connection:
        assert compute_index_status(connection, place.index_id) is IndexStatus.READY


# --- Failure side effects (section 6.11) ----------------------------------------------------------


@dataclass(frozen=True)
class FailingItem:
    item_id: UUID
    version_id: UUID
    chunk_set_id: UUID


def failing_item(
    engines: Engines,
    index_id: UUID,
    project_id: UUID,
    physical_index_id: UUID,
    *,
    version_state: str = "pending",
) -> FailingItem:
    """An `ingest` item of a new document's version 1 into `physical_index_id`, with its
    `building` chunk set."""
    document = document_row(index_id)
    version = document_version_row(
        document["id"], index_id, project_id, state=version_state, committed_at=sql_now()
    )
    chunk_set = chunk_set_row(document["id"], version["id"], physical_index_id)
    run = ingestion_run_row(index_id)
    item = ingestion_run_item_row(
        run["id"],
        index_id,
        document_id=document["id"],
        document_version_id=version["id"],
        physical_index_id=physical_index_id,
        chunk_set_id=chunk_set["id"],
    )
    put(
        engines.admin,
        (documents, document),
        (document_versions, version),
        (chunk_sets, chunk_set),
        (ingestion_runs, run),
        (ingestion_run_items, item),
    )
    return FailingItem(item["id"], version["id"], chunk_set["id"])


def fail_as_operator(engines: Engines, item_id: UUID) -> FailureSideEffects:
    with engines.operator.begin() as connection:
        return apply_failure_side_effects(connection, item_id, "processing_interrupted")


def states_of(engines: Engines, failing: FailingItem) -> tuple[Any, ...]:
    with engines.admin.connect() as connection:
        chunk_set = connection.execute(
            select(chunk_sets.c.state, chunk_sets.c.retired_at).where(
                chunk_sets.c.id == failing.chunk_set_id
            )
        ).one()
        version = connection.execute(
            select(
                document_versions.c.state,
                document_versions.c.failure_code,
                document_versions.c.failed_at,
            ).where(document_versions.c.id == failing.version_id)
        ).one()
    return (
        chunk_set.state,
        chunk_set.retired_at is not None,
        version.state,
        version.failure_code,
        version.failed_at is not None,
    )


def unserved_index(engines: Engines, physical_state: str) -> tuple[UUID, UUID, UUID]:
    """A logical index with no active physical index and one physical index in
    `physical_state`; returns the project, logical and physical index IDs."""
    project = project_row()
    index = search_index_row(status="failed")
    retiring: dict[str, Any] = (
        {"retiring_at": sql_now(), "delete_after": sql_now()}
        if physical_state == "retiring"
        else {}
    )
    physical = physical_index_row(index["id"], state=physical_state, **retiring)
    put(
        engines.admin,
        (projects, project),
        (search_indexes, index),
        (physical_indexes, physical),
    )
    return project["id"], index["id"], physical["id"]


def test_a_failure_into_the_active_index_retires_the_build_and_fails_the_version(
    engines: Engines,
) -> None:
    place = ledger(engines)
    failing = failing_item(engines, place.index_id, place.project_id, place.active_id)
    assert fail_as_operator(engines, failing.item_id) == FailureSideEffects(
        chunk_set_retired=True, version_failed=True
    )
    assert states_of(engines, failing) == (
        "retired",
        True,
        "failed",
        "processing_interrupted",
        True,
    )
    # A second application finds nothing left to change.
    assert fail_as_operator(engines, failing.item_id) == FailureSideEffects(
        chunk_set_retired=False, version_failed=False
    )


def test_a_failed_reingest_of_a_current_version_changes_only_its_chunk_set(
    engines: Engines,
) -> None:
    place = ledger(engines)
    failing = failing_item(
        engines, place.index_id, place.project_id, place.active_id, version_state="ingested"
    )
    assert fail_as_operator(engines, failing.item_id) == FailureSideEffects(
        chunk_set_retired=True, version_failed=False
    )
    assert states_of(engines, failing) == ("retired", True, "ingested", None, False)


def test_a_failure_into_a_rebuild_beside_an_active_index_leaves_the_version(
    engines: Engines,
) -> None:
    place = ledger(engines)
    building = physical_index_row(place.index_id, generation=2)
    put(engines.admin, (physical_indexes, building))
    failing = failing_item(engines, place.index_id, place.project_id, building["id"])
    assert fail_as_operator(engines, failing.item_id) == FailureSideEffects(
        chunk_set_retired=True, version_failed=False
    )
    assert states_of(engines, failing)[2] == "pending"


def test_with_no_active_index_a_failure_into_the_building_index_fails_the_version(
    engines: Engines,
) -> None:
    project_id, index_id, building_id = unserved_index(engines, "building")
    failing = failing_item(engines, index_id, project_id, building_id)
    assert fail_as_operator(engines, failing.item_id) == FailureSideEffects(
        chunk_set_retired=True, version_failed=True
    )
    assert states_of(engines, failing)[2:4] == ("failed", "processing_interrupted")


def test_with_no_active_index_the_building_check_reads_the_failed_item_only(
    engines: Engines,
) -> None:
    # Another logical index's item targets its building index; this item targets a retiring
    # one, so its version stays as it is.
    other_project, other_index, other_building = unserved_index(engines, "building")
    failing_item(engines, other_index, other_project, other_building)
    project_id, index_id, retiring_id = unserved_index(engines, "retiring")
    failing = failing_item(engines, index_id, project_id, retiring_id)
    assert fail_as_operator(engines, failing.item_id) == FailureSideEffects(
        chunk_set_retired=True, version_failed=False
    )
    assert states_of(engines, failing)[2] == "pending"


def test_an_item_without_a_build_or_a_version_changes_nothing(engines: Engines) -> None:
    place = ledger(engines)
    run = ingestion_run_row(place.index_id, kind="maintenance")
    item = ingestion_run_item_row(
        run["id"],
        place.index_id,
        physical_index_id=place.active_id,
        operation="delete_physical_index",
    )
    put(engines.admin, (ingestion_runs, run), (ingestion_run_items, item))
    assert fail_as_operator(engines, item["id"]) == FailureSideEffects(
        chunk_set_retired=False, version_failed=False
    )
