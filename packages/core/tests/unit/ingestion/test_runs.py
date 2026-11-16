"""Unit tests of run creation, the failure side effects and the logical index status (T-111;
`14-ingestion.md` sections 2.6, 2.8, 6.11, 7, 8.1 to 8.7 and 8.11; `80-data-model.md` sections
2.16, 2.17 and 8.4).

The functions run on a scripted connection: every statement is compiled for SQL Server, so a
statement the dialect can't render fails here, and each read or write is answered from the
script in order. Inserts are recorded and answered without consuming the script. The behaviour
against a real SQL Server is proven by `packages/core/tests/integration/ingestion/test_runs.py`.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Connection
from sqlalchemy.dialects import mssql
from sqlalchemy.sql.dml import Insert
from sqlalchemy.sql.elements import ClauseElement

from docchat_core.errors import ITEM_CODES
from docchat_core.ingestion.methods import PAGEWISE_V1, RECURSIVE_V1
from docchat_core.ingestion.models import (
    IndexStatus,
    IngestionRefusedError,
    ItemOperation,
    ReingestMode,
    RunKind,
    RunPriority,
    SubmitOutcome,
    TraceContext,
)
from docchat_core.ingestion.names import physical_index_name
from docchat_core.ingestion.runs import (
    RUN_KIND_PRIORITY,
    FailureSideEffects,
    IndexSettings,
    IndexStatusFacts,
    NewItem,
    PhysicalIndexSettings,
    apply_failure_side_effects,
    classify_submissions,
    compute_index_status,
    create_document_delete_run,
    create_document_reingest_run,
    create_index_create_run,
    create_index_delete_run,
    create_maintenance_run,
    create_reingest_run,
    create_upload_run,
    decide_index_status,
    has_capacity,
    run_priority,
    serves_settings,
)
from docchat_core.ingestion.settings import DEFAULT_EMBEDDING_MODELS, EmbeddingModelCatalog

TRACE = TraceContext("00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01", "a=1")
ROW_VERSION = b"\x00\x00\x00\x00\x00\x00\x07\xd1"
OTHER_ROW_VERSION = b"\x00\x00\x00\x00\x00\x00\x07\xd2"
MODEL = "text-embedding-3-large"
ANALYZER = "standard.lucene"
CATALOG = EmbeddingModelCatalog.model_construct(openai_embedding_models=DEFAULT_EMBEDDING_MODELS)


@dataclass
class Result:
    """What one scripted statement answers: its rows (values, or rows with attributes) and its
    row count."""

    rows: list[Any] = field(default_factory=list)
    rowcount: int = 0

    def first(self) -> Any:
        return self.rows[0] if self.rows else None

    def one(self) -> Any:
        assert len(self.rows) == 1
        return self.rows[0]

    def all(self) -> list[Any]:
        return list(self.rows)

    def scalar_one(self) -> Any:
        return self.one()

    def scalar_one_or_none(self) -> Any:
        return self.first()

    def scalars(self) -> list[Any]:
        return list(self.rows)


DIALECT = mssql.dialect()  # type: ignore[no-untyped-call]


@dataclass
class Executed:
    sql: str
    statement: ClauseElement
    parameters: Any

    @property
    def values(self) -> dict[str, Any]:
        """The bound values of a single-row statement."""
        return dict(self.statement.compile(dialect=DIALECT).params)


class ScriptedConnection:
    def __init__(self, *results: Result) -> None:
        self.script = deque(results)
        self.executed: list[Executed] = []

    def execute(self, statement: ClauseElement, parameters: Any = None) -> Result:
        compiled = statement.compile(dialect=DIALECT)
        self.executed.append(Executed(str(compiled), statement, parameters))
        if isinstance(statement, Insert):
            return Result(rowcount=len(parameters) if isinstance(parameters, list) else 1)
        assert self.script, f"no scripted answer for: {compiled}"
        return self.script.popleft()

    def inserts(self, table: str) -> list[Executed]:
        return [
            entry for entry in self.executed if entry.sql.startswith(f"INSERT INTO app.{table} ")
        ]

    def item_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for entry in self.inserts("ingestion_run_items"):
            rows.extend(entry.parameters)
        return rows

    def run_values(self) -> dict[str, Any]:
        (entry,) = self.inserts("ingestion_runs")
        return entry.values

    def finished(self) -> None:
        assert not self.script, "the script has unused answers"


def connection_of(fake: ScriptedConnection) -> Connection:
    return cast("Connection", fake)


def rows(*values: Any) -> Result:
    return Result(rows=list(values))


def count(value: int) -> Result:
    return Result(rowcount=value)


def index_row(
    status: str = "ready",
    *,
    row_version: bytes = ROW_VERSION,
    active: UUID | None = None,
    model: str = MODEL,
    dimensions: int = 1024,
    analyzer: str = ANALYZER,
) -> Result:
    return rows(
        SimpleNamespace(
            status=status,
            row_version=row_version,
            embedding_model=model,
            embedding_dimensions=dimensions,
            language_analyzer=analyzer,
            active_physical_index_id=active,
        )
    )


def physical_settings(
    *, model: str = MODEL, dimensions: int = 1024, analyzer: str = ANALYZER, schema: int = 1
) -> Result:
    return rows(
        SimpleNamespace(
            embedding_model=model,
            embedding_dimensions=dimensions,
            language_analyzer=analyzer,
            schema_version=schema,
        )
    )


# --- Priorities and items -------------------------------------------------------------------------


def test_priorities_follow_the_run_kind() -> None:
    assert {kind for kind, priority in RUN_KIND_PRIORITY.items() if priority == 1} == {
        RunKind.INDEX_REINGEST,
        RunKind.INDEX_REBUILD,
        RunKind.INDEX_DELETE,
    }
    assert set(RUN_KIND_PRIORITY) == set(RunKind)
    assert run_priority(RunKind.MAINTENANCE) is RunPriority.INTERACTIVE
    assert run_priority(RunKind.MAINTENANCE, rerender=True) is RunPriority.BULK
    with pytest.raises(ValueError, match="maintenance"):
        run_priority(RunKind.UPLOAD, rerender=True)


@pytest.mark.parametrize(
    ("operation", "targets"),
    [
        (ItemOperation.INGEST, {"document_id", "document_version_id", "physical_index_id"}),
        (ItemOperation.CLEANUP_DOCUMENT, {"document_id"}),
        (
            ItemOperation.RERENDER,
            {"document_id", "document_version_id", "physical_index_id", "chunk_set_id"},
        ),
        (ItemOperation.CREATE_PHYSICAL_INDEX, {"physical_index_id"}),
        (ItemOperation.DELETE_PHYSICAL_INDEX, {"physical_index_id"}),
        (ItemOperation.PURGE_INDEX_BLOBS, set()),
    ],
)
def test_an_item_carries_exactly_its_operations_targets(
    operation: ItemOperation, targets: set[str]
) -> None:
    complete: dict[str, Any] = dict.fromkeys(targets, uuid4())
    NewItem(operation, **complete)
    for missing in targets:
        partial: dict[str, Any] = {name: uuid4() for name in targets - {missing}}
        with pytest.raises(ValueError, match="targets"):
            NewItem(operation, **partial)


@pytest.mark.parametrize(
    ("operation", "extra"),
    [
        (ItemOperation.CLEANUP_DOCUMENT, "document_version_id"),
        (ItemOperation.CREATE_PHYSICAL_INDEX, "document_id"),
        (ItemOperation.PURGE_INDEX_BLOBS, "physical_index_id"),
        (ItemOperation.PURGE_INDEX_BLOBS, "document_id"),
    ],
)
def test_an_item_carries_no_target_its_operation_lacks(
    operation: ItemOperation, extra: str
) -> None:
    base: dict[str, Any] = {}
    if operation is ItemOperation.CLEANUP_DOCUMENT:
        base = {"document_id": uuid4()}
    if operation is ItemOperation.CREATE_PHYSICAL_INDEX:
        base = {"physical_index_id": uuid4()}
    with pytest.raises(ValueError, match="targets"):
        NewItem(operation, **{**base, extra: uuid4()})


def test_a_maintenance_run_has_no_requester_and_follows_its_items() -> None:
    index, physical = uuid4(), uuid4()
    fake = ScriptedConnection()
    created = create_maintenance_run(
        connection_of(fake),
        index_id=index,
        items=[NewItem(ItemOperation.DELETE_PHYSICAL_INDEX, physical_index_id=physical)],
    )
    run = fake.run_values()
    assert (run["kind"], run["priority"], run["requested_by"], run["traceparent"]) == (
        "maintenance",
        0,
        None,
        None,
    )
    (item,) = fake.item_rows()
    assert item["id"] == created.item_ids[0]
    assert (item["operation"], item["physical_index_id"], item["run_id"]) == (
        "delete_physical_index",
        physical,
        created.run_id,
    )


def test_a_maintenance_run_of_rerender_items_is_bulk() -> None:
    fake = ScriptedConnection()
    not_before = datetime(2026, 10, 30, tzinfo=UTC)
    create_maintenance_run(
        connection_of(fake),
        index_id=uuid4(),
        items=[
            NewItem(
                ItemOperation.RERENDER,
                document_id=uuid4(),
                document_version_id=uuid4(),
                physical_index_id=uuid4(),
                chunk_set_id=uuid4(),
                not_before=not_before,
            )
        ],
    )
    assert fake.run_values()["priority"] == 1
    assert fake.item_rows()[0]["priority"] == 1
    assert fake.item_rows()[0]["not_before"] == not_before


def test_a_maintenance_run_needs_items_of_one_kind() -> None:
    fake = connection_of(ScriptedConnection())
    with pytest.raises(ValueError, match="with its items"):
        create_maintenance_run(fake, index_id=uuid4(), items=[])
    rerender = NewItem(
        ItemOperation.RERENDER,
        document_id=uuid4(),
        document_version_id=uuid4(),
        physical_index_id=uuid4(),
        chunk_set_id=uuid4(),
    )
    with pytest.raises(ValueError, match="nothing else"):
        create_maintenance_run(
            fake, index_id=uuid4(), items=[rerender, NewItem(ItemOperation.PURGE_INDEX_BLOBS)]
        )


# --- Upload ---------------------------------------------------------------------------------------


def test_classification_follows_the_documented_order() -> None:
    found, pending, uploading, queued = uuid4(), uuid4(), uuid4(), uuid4()
    results = classify_submissions(
        [found, pending, uploading, queued],
        {pending: "pending", uploading: "uploading", queued: "pending"},
        {queued, uploading},
    )
    assert [result.outcome for result in results] == [
        SubmitOutcome.NOT_FOUND,
        SubmitOutcome.QUEUED,
        SubmitOutcome.NOT_PENDING,
        SubmitOutcome.ALREADY_QUEUED,
    ]


def test_an_upload_run_queues_the_pending_versions() -> None:
    index, active, requester = uuid4(), uuid4(), uuid4()
    pending, uploading, queued, absent = uuid4(), uuid4(), uuid4(), uuid4()
    documents = {pending: uuid4(), uploading: uuid4(), queued: uuid4()}
    fake = ScriptedConnection(
        rows(
            *(
                SimpleNamespace(id=version, document_id=documents[version], state=state)
                for version, state in (
                    (pending, "pending"),
                    (uploading, "uploading"),
                    (queued, "pending"),
                )
            )
        ),
        rows(queued),
        rows(active),
    )
    result = create_upload_run(
        connection_of(fake),
        index_id=index,
        version_ids=[absent, pending, uploading, queued],
        requested_by=requester,
        trace=TRACE,
    )
    fake.finished()
    assert result.run_id is not None
    assert [entry.outcome for entry in result.results] == [
        SubmitOutcome.NOT_FOUND,
        SubmitOutcome.QUEUED,
        SubmitOutcome.NOT_PENDING,
        SubmitOutcome.ALREADY_QUEUED,
    ]
    assert (result.item_count, result.not_queued_count) == (1, 3)
    assert "FROM app.document_versions WITH (UPDLOCK, ROWLOCK)" in fake.executed[0].sql
    assert "state IN (N'queued', N'running')" in fake.executed[1].sql
    run = fake.run_values()
    assert run == {
        "id": result.run_id,
        "kind": "upload",
        "search_index_id": index,
        "priority": 0,
        "state": "queued",
        "requested_by": requester,
        "traceparent": TRACE.traceparent,
        "tracestate": "a=1",
    }
    (item,) = fake.item_rows()
    assert item["operation"] == "ingest"
    assert (item["document_id"], item["document_version_id"], item["physical_index_id"]) == (
        documents[pending],
        pending,
        active,
    )
    assert item["priority"] == 0


def test_an_upload_waits_for_a_building_index_without_an_active_one() -> None:
    version, building = uuid4(), uuid4()
    fake = ScriptedConnection(
        rows(SimpleNamespace(id=version, document_id=uuid4(), state="pending")),
        rows(),
        rows(None),
        rows(building),
    )
    result = create_upload_run(
        connection_of(fake),
        index_id=uuid4(),
        version_ids=[version],
        requested_by=uuid4(),
        trace=None,
    )
    fake.finished()
    assert fake.item_rows()[0]["physical_index_id"] == building
    assert fake.run_values()["traceparent"] is None
    assert result.item_count == 1


def test_nothing_queued_creates_no_run() -> None:
    version = uuid4()
    fake = ScriptedConnection(rows(), rows())
    result = create_upload_run(
        connection_of(fake),
        index_id=uuid4(),
        version_ids=[version],
        requested_by=uuid4(),
        trace=None,
    )
    fake.finished()
    assert result.run_id is None
    assert result.results[0].outcome is SubmitOutcome.NOT_FOUND
    assert fake.inserts("ingestion_runs") == []


def test_an_index_without_a_target_takes_no_upload() -> None:
    version = uuid4()
    fake = ScriptedConnection(
        rows(SimpleNamespace(id=version, document_id=uuid4(), state="pending")),
        rows(),
        rows(None),
        rows(),
    )
    with pytest.raises(IngestionRefusedError) as refused:
        create_upload_run(
            connection_of(fake),
            index_id=uuid4(),
            version_ids=[version],
            requested_by=uuid4(),
            trace=None,
        )
    assert refused.value.code == "index_not_accepting_uploads"


@pytest.mark.parametrize("versions", [[], [uuid4()] * 2, [uuid4() for _ in range(101)]])
def test_a_submit_names_1_to_100_distinct_versions(versions: list[UUID]) -> None:
    with pytest.raises(ValueError, match="submit"):
        create_upload_run(
            connection_of(ScriptedConnection()),
            index_id=uuid4(),
            version_ids=versions,
            requested_by=uuid4(),
            trace=None,
        )


# --- Document re-ingest ---------------------------------------------------------------------------


def test_a_document_reingest_targets_the_active_index() -> None:
    index, document, version, active = uuid4(), uuid4(), uuid4(), uuid4()
    fake = ScriptedConnection(index_row(active=active), count(1), rows(version), rows(), rows())
    result = create_document_reingest_run(
        connection_of(fake),
        index_id=index,
        document_id=document,
        row_version=ROW_VERSION,
        requested_by=uuid4(),
        trace=TRACE,
    )
    fake.finished()
    assert (result.created, result.item_count) == (True, 1)
    assert fake.run_values()["kind"] == "document_reingest"
    (item,) = fake.item_rows()
    assert (item["document_version_id"], item["physical_index_id"]) == (version, active)
    assert "updated_at=SYSUTCDATETIME()" in fake.executed[1].sql


def test_a_document_reingest_targets_the_building_index_during_a_rebuild() -> None:
    building = uuid4()
    fake = ScriptedConnection(
        index_row("rebuilding", active=uuid4()), count(1), rows(uuid4()), rows(building), rows()
    )
    create_document_reingest_run(
        connection_of(fake),
        index_id=uuid4(),
        document_id=uuid4(),
        row_version=ROW_VERSION,
        requested_by=uuid4(),
        trace=None,
    )
    assert fake.item_rows()[0]["physical_index_id"] == building


def test_an_open_item_answers_with_its_newest_run() -> None:
    open_run = uuid4()
    fake = ScriptedConnection(
        index_row(active=uuid4()), count(1), rows(uuid4()), rows(), rows(open_run)
    )
    result = create_document_reingest_run(
        connection_of(fake),
        index_id=uuid4(),
        document_id=uuid4(),
        row_version=ROW_VERSION,
        requested_by=uuid4(),
        trace=None,
    )
    assert (result.run_id, result.created, result.item_count) == (open_run, False, 0)
    assert fake.inserts("ingestion_runs") == []
    assert "ORDER BY app.ingestion_runs.created_at DESC" in fake.executed[-1].sql


@pytest.mark.parametrize(
    ("script", "code"),
    [
        ((rows(),), "index_not_found"),
        ((index_row("deleted"),), "index_not_found"),
        ((index_row(), count(0), rows()), "document_not_found"),
        ((index_row(), count(0), rows(SimpleNamespace(is_deleted=True))), "document_not_found"),
        ((index_row(), count(0), rows(SimpleNamespace(is_deleted=False))), "precondition_failed"),
        ((index_row(active=uuid4()), count(1), rows(None)), "no_current_version"),
        ((index_row("failed"), count(1), rows(uuid4())), "index_busy"),
        ((index_row("deleting"), count(1), rows(uuid4())), "index_busy"),
        ((index_row(active=None), count(1), rows(uuid4()), rows()), "index_busy"),
    ],
)
def test_document_reingest_refusals(script: tuple[Result, ...], code: str) -> None:
    fake = ScriptedConnection(*script)
    with pytest.raises(IngestionRefusedError) as refused:
        create_document_reingest_run(
            connection_of(fake),
            index_id=uuid4(),
            document_id=uuid4(),
            row_version=ROW_VERSION,
            requested_by=uuid4(),
            trace=None,
        )
    assert refused.value.code == code
    fake.finished()


# --- Document delete ------------------------------------------------------------------------------


def test_a_document_delete_hides_the_document_and_queues_its_clean_up() -> None:
    index, document, requester = uuid4(), uuid4(), uuid4()
    fake = ScriptedConnection(
        index_row(active=uuid4()),
        count(1),
        count(2),
        count(1),
        count(1),
        count(3),
        count(4),
    )
    result = create_document_delete_run(
        connection_of(fake),
        index_id=index,
        document_id=document,
        row_version=ROW_VERSION,
        requested_by=requester,
        trace=TRACE,
    )
    fake.finished()
    assert (result.superseded_version_count, result.cancelled_item_count) == (2, 4)
    sql = [entry.sql for entry in fake.executed]
    assert "is_deleted=1" in sql[1]
    assert "current_version_id=" in sql[1]
    assert "state=N'superseded'" in sql[2]
    assert "N'pending', N'ingesting', N'ingested'" in sql[2]
    assert "state=N'failed'" in sql[3]
    assert "state = N'uploading'" in sql[3]
    assert sql[4].startswith("DELETE FROM app.served_chunk_sets")
    assert "state=N'retired'" in sql[5]
    assert "N'building', N'ready', N'served'" in sql[5]
    assert "state=N'cancelled'" in sql[6]
    cancel = fake.executed[6].values
    assert cancel["outcome_code"] == "document_deleted"
    assert cancel["outcome_message"] == ITEM_CODES["document_deleted"].message
    assert fake.executed[3].values["failure_code"] == "document_deleted"
    assert fake.run_values()["kind"] == "document_delete"
    (item,) = fake.item_rows()
    assert (item["operation"], item["document_id"], item["document_version_id"]) == (
        "cleanup_document",
        document,
        None,
    )


@pytest.mark.parametrize(
    ("script", "code"),
    [
        ((rows(),), "index_not_found"),
        ((index_row(), count(0), rows()), "document_not_found"),
        ((index_row(), count(0), rows(SimpleNamespace(is_deleted=False))), "precondition_failed"),
        ((index_row("deleting"), count(1)), "index_busy"),
    ],
)
def test_document_delete_refusals(script: tuple[Result, ...], code: str) -> None:
    fake = ScriptedConnection(*script)
    with pytest.raises(IngestionRefusedError) as refused:
        create_document_delete_run(
            connection_of(fake),
            index_id=uuid4(),
            document_id=uuid4(),
            row_version=OTHER_ROW_VERSION,
            requested_by=uuid4(),
            trace=None,
        )
    assert refused.value.code == code
    fake.finished()


# --- Index creation -------------------------------------------------------------------------------


def settings(**changes: Any) -> IndexSettings:
    values: dict[str, Any] = {
        "method": RECURSIVE_V1,
        "parameters": RECURSIVE_V1.validate_parameters({"targetTokens": 600}),
        "embedding": DEFAULT_EMBEDDING_MODELS[0],
        "dimensions": 1024,
        "analyzer": ANALYZER,
    }
    values.update(changes)
    return IndexSettings(**values)


def test_index_settings_are_checked_against_the_catalogue() -> None:
    with pytest.raises(TypeError):
        settings(parameters=PAGEWISE_V1.default_parameters())
    with pytest.raises(ValueError, match="dimensions"):
        settings(dimensions=512)
    with pytest.raises(ValueError, match="analyzer"):
        settings(analyzer="xx.lucene")


def test_an_index_create_writes_the_index_its_first_generation_and_the_run() -> None:
    creator = uuid4()
    fake = ScriptedConnection(rows(13))
    chosen = settings()
    result = create_index_create_run(
        connection_of(fake),
        display_name="Policies",
        description=None,
        settings=chosen,
        max_indexes=15,
        created_by=creator,
        trace=TRACE,
    )
    fake.finished()
    assert "state != N'deleted'" in fake.executed[0].sql
    (index_insert,) = fake.inserts("search_indexes")
    index = index_insert.values
    assert index["id"] == result.index_id
    assert index["status"] == "provisioning"
    assert index["method_key"] == "recursive"
    assert index["method_parameters"] == (
        '{"targetTokens":600,"overlapPercent":25,"tableMaxTokens":1600}'
    )
    assert index["method_parameter_hash"] == RECURSIVE_V1.parameter_hash(chosen.parameters)
    assert (index["created_by"], index["updated_by"]) == (creator, creator)
    (physical_insert,) = fake.inserts("physical_indexes")
    physical = physical_insert.values
    assert physical["id"] == result.physical_index_id
    assert physical["generation"] == 1
    assert physical["name"] == physical_index_name(result.index_id, 1)
    assert physical["state"] == "building"
    assert physical["build_run_id"] == result.run_id
    assert physical["schema_version"] == 1
    assert (physical["embedding_model_version"], physical["embedding_encoding"]) == (
        "1",
        "cl100k_base",
    )
    assert physical["embedding_max_input_tokens"] == 8192
    assert physical["query_embedding_deployment"] == "embed-query-te3-large-v1"
    assert physical["ingestion_embedding_deployment"] == "embed-ingest-te3-large-v1"
    run = fake.run_values()
    assert (run["kind"], run["priority"], run["id"]) == ("index_create", 0, result.run_id)
    (item,) = fake.item_rows()
    assert (item["operation"], item["physical_index_id"]) == (
        "create_physical_index",
        result.physical_index_id,
    )
    tables = [entry.sql.split()[2] for entry in fake.executed if entry.sql.startswith("INSERT")]
    assert tables == [
        "app.search_indexes",
        "app.ingestion_runs",
        "app.physical_indexes",
        "app.ingestion_run_items",
    ]


def test_index_creation_keeps_one_slot_free() -> None:
    fake = ScriptedConnection(rows(14))
    with pytest.raises(IngestionRefusedError) as refused:
        create_index_create_run(
            connection_of(fake),
            display_name="Full",
            description="d",
            settings=settings(),
            max_indexes=15,
            created_by=uuid4(),
            trace=None,
        )
    assert refused.value.code == "search_index_capacity"
    assert fake.inserts("search_indexes") == []


def test_capacity_rules() -> None:
    assert has_capacity(13, 15, keep_free=True)
    assert not has_capacity(14, 15, keep_free=True)
    assert has_capacity(14, 15, keep_free=False)
    assert not has_capacity(15, 15, keep_free=False)


# --- Re-ingest and rebuild ------------------------------------------------------------------------


def live(*pairs: tuple[UUID, UUID]) -> Result:
    return rows(
        *(SimpleNamespace(id=document, current_version_id=version) for document, version in pairs)
    )


def test_a_reingest_in_place_queues_every_live_current_version() -> None:
    index, active, requester = uuid4(), uuid4(), uuid4()
    first, second = (uuid4(), uuid4()), (uuid4(), uuid4())
    fake = ScriptedConnection(
        index_row("reingest_required", active=active),
        physical_settings(),
        count(1),
        live(first, second),
    )
    result = create_reingest_run(
        connection_of(fake),
        index_id=index,
        row_version=ROW_VERSION,
        requested_by=requester,
        trace=TRACE,
        max_indexes=15,
        catalog=CATALOG,
    )
    fake.finished()
    assert result.mode is ReingestMode.SAME_INDEX
    assert result.status is IndexStatus.REINGESTING
    assert (result.item_count, result.physical_index_id) == (2, active)
    assert "WITH (UPDLOCK, ROWLOCK)" in fake.executed[0].sql
    assert "status=:status" in fake.executed[2].sql
    assert fake.executed[2].values["status"] == "reingesting"
    assert "NOT (EXISTS" in fake.executed[3].sql
    run = fake.run_values()
    assert (run["kind"], run["priority"]) == ("index_reingest", 1)
    assert [(row["document_id"], row["document_version_id"]) for row in fake.item_rows()] == [
        first,
        second,
    ]
    assert {row["physical_index_id"] for row in fake.item_rows()} == {active}
    assert {row["priority"] for row in fake.item_rows()} == {1}


def test_a_reingest_with_no_live_document_creates_an_empty_run() -> None:
    fake = ScriptedConnection(index_row(active=uuid4()), physical_settings(), count(1), live())
    result = create_reingest_run(
        connection_of(fake),
        index_id=uuid4(),
        row_version=ROW_VERSION,
        requested_by=uuid4(),
        trace=None,
        max_indexes=15,
        catalog=CATALOG,
    )
    assert result.item_count == 0
    assert fake.item_rows() == []
    assert len(fake.inserts("ingestion_runs")) == 1


@pytest.mark.parametrize(
    "active",
    [
        physical_settings(analyzer="en.microsoft"),
        physical_settings(dimensions=3072),
        physical_settings(model="text-embedding-3-small"),
        physical_settings(schema=0),
    ],
)
def test_a_setting_the_active_index_doesnt_serve_builds_a_new_generation(active: Result) -> None:
    index, old, requester = uuid4(), uuid4(), uuid4()
    document = (uuid4(), uuid4())
    fake = ScriptedConnection(
        index_row(active=old),
        active,
        rows(3),
        count(1),
        rows(),
        rows(1),
        live(document),
    )
    result = create_reingest_run(
        connection_of(fake),
        index_id=index,
        row_version=ROW_VERSION,
        requested_by=requester,
        trace=TRACE,
        max_indexes=5,
        catalog=CATALOG,
    )
    fake.finished()
    assert result.mode is ReingestMode.NEW_PHYSICAL_INDEX
    assert result.status is IndexStatus.REBUILDING
    assert result.item_count == 1
    assert fake.executed[3].values["status"] == "rebuilding"
    (physical_insert,) = fake.inserts("physical_indexes")
    physical = physical_insert.values
    assert physical["id"] == result.physical_index_id
    assert physical["generation"] == 2
    assert physical["name"] == physical_index_name(index, 2)
    assert physical["build_run_id"] == result.run_id
    assert (physical["embedding_dimensions"], physical["language_analyzer"]) == (1024, ANALYZER)
    run = fake.run_values()
    assert (run["kind"], run["priority"]) == ("index_rebuild", 1)
    items = fake.item_rows()
    assert [row["operation"] for row in items] == ["create_physical_index", "ingest"]
    assert {row["physical_index_id"] for row in items} == {result.physical_index_id}
    assert (items[1]["document_id"], items[1]["document_version_id"]) == document


def test_a_failed_index_retires_its_leftover_and_moves_its_queued_items() -> None:
    leftover = uuid4()
    fake = ScriptedConnection(
        index_row("failed", active=None),
        rows(2),
        count(1),
        rows(leftover),
        count(1),
        rows(1),
        count(2),
        live(),
    )
    result = create_reingest_run(
        connection_of(fake),
        index_id=uuid4(),
        row_version=ROW_VERSION,
        requested_by=uuid4(),
        trace=None,
        max_indexes=3,
        catalog=CATALOG,
    )
    fake.finished()
    assert result.mode is ReingestMode.NEW_PHYSICAL_INDEX
    retire = fake.executed[4]
    assert "state=N'retiring'" in retire.sql
    assert "delete_after=SYSUTCDATETIME()" in retire.sql
    assert "build_run_id=" in retire.sql
    move = fake.executed[8]
    assert move.sql.startswith("UPDATE app.ingestion_run_items SET physical_index_id=")
    assert move.values["physical_index_id"] == result.physical_index_id
    assert leftover in move.values.values()
    assert [row["operation"] for row in fake.item_rows()] == ["create_physical_index"]


@pytest.mark.parametrize(
    ("script", "max_indexes", "code"),
    [
        ((rows(),), 15, "index_not_found"),
        ((index_row("deleted"),), 15, "index_not_found"),
        ((index_row(row_version=OTHER_ROW_VERSION),), 15, "precondition_failed"),
        ((index_row("reingesting", active=uuid4()),), 15, "index_busy"),
        ((index_row("rebuilding", active=uuid4()),), 15, "index_busy"),
        ((index_row("provisioning"),), 15, "index_busy"),
        ((index_row(active=uuid4()), physical_settings(), count(0)), 15, "precondition_failed"),
        ((index_row("failed"), rows(5)), 5, "search_index_capacity"),
        (
            (index_row("failed", model="text-embedding-3-small"), rows(1)),
            5,
            "unsupported_embedding_model",
        ),
        ((index_row("failed", dimensions=512), rows(1)), 5, "unsupported_embedding_model"),
    ],
)
def test_reingest_refusals(script: tuple[Result, ...], max_indexes: int, code: str) -> None:
    fake = ScriptedConnection(*script)
    with pytest.raises(IngestionRefusedError) as refused:
        create_reingest_run(
            connection_of(fake),
            index_id=uuid4(),
            row_version=ROW_VERSION,
            requested_by=uuid4(),
            trace=None,
            max_indexes=max_indexes,
            catalog=CATALOG,
        )
    assert refused.value.code == code
    fake.finished()


def test_a_rebuild_may_take_the_slot_creation_keeps_free() -> None:
    fake = ScriptedConnection(index_row("failed"), rows(4), count(1), rows(), rows(1), live())
    result = create_reingest_run(
        connection_of(fake),
        index_id=uuid4(),
        row_version=ROW_VERSION,
        requested_by=uuid4(),
        trace=None,
        max_indexes=5,
        catalog=CATALOG,
    )
    assert result.mode is ReingestMode.NEW_PHYSICAL_INDEX


def test_serving_needs_every_setting_and_the_current_schema() -> None:
    active = PhysicalIndexSettings(MODEL, 1024, ANALYZER, 1)
    kwargs = {"embedding_model": MODEL, "embedding_dimensions": 1024, "language_analyzer": ANALYZER}
    assert serves_settings(active, **kwargs)  # type: ignore[arg-type]
    assert not serves_settings(None, **kwargs)  # type: ignore[arg-type]
    assert not serves_settings(
        PhysicalIndexSettings(MODEL, 1024, ANALYZER, 2),
        **kwargs,  # type: ignore[arg-type]
    )


# --- Index delete ---------------------------------------------------------------------------------


def test_an_index_delete_deletes_every_document_and_every_physical_index() -> None:
    index, first, second, requester = uuid4(), uuid4(), uuid4(), uuid4()
    fake = ScriptedConnection(
        index_row(active=first),
        rows(),
        count(1),
        count(5),
        count(3),
        count(1),
        count(2),
        count(2),
        count(1),
        rows(first, second),
    )
    result = create_index_delete_run(
        connection_of(fake),
        index_id=index,
        row_version=ROW_VERSION,
        requested_by=requester,
        trace=TRACE,
    )
    fake.finished()
    assert (result.physical_index_count, result.document_count) == (2, 5)
    sql = [entry.sql for entry in fake.executed]
    assert "FROM app.project_indexes" in sql[1]
    assert fake.executed[2].values["status"] == "deleting"
    assert "is_deleted=1" in sql[3]
    assert "state=N'superseded'" in sql[4]
    assert "state=N'failed'" in sql[5]
    assert sql[6].startswith("DELETE FROM app.served_chunk_sets")
    assert "IN (SELECT" in sql[6]
    assert "state=N'retired'" in sql[7]
    assert fake.executed[8].values["outcome_code"] == "index_deleted"
    assert fake.executed[8].values["outcome_message"] == ITEM_CODES["index_deleted"].message
    assert "ORDER BY app.physical_indexes.generation" in sql[9]
    run = fake.run_values()
    assert (run["kind"], run["priority"]) == ("index_delete", 1)
    assert [(row["operation"], row["physical_index_id"]) for row in fake.item_rows()] == [
        ("delete_physical_index", first),
        ("delete_physical_index", second),
        ("purge_index_blobs", None),
    ]


@pytest.mark.parametrize(
    ("script", "code"),
    [
        ((rows(),), "index_not_found"),
        ((index_row(row_version=OTHER_ROW_VERSION),), "precondition_failed"),
        ((index_row(), rows(uuid4())), "index_mapped"),
        ((index_row("provisioning"), rows()), "index_busy"),
        ((index_row("deleting"), rows()), "index_busy"),
        ((index_row(), rows(), count(0)), "precondition_failed"),
    ],
)
def test_index_delete_refusals(script: tuple[Result, ...], code: str) -> None:
    fake = ScriptedConnection(*script)
    with pytest.raises(IngestionRefusedError) as refused:
        create_index_delete_run(
            connection_of(fake),
            index_id=uuid4(),
            row_version=ROW_VERSION,
            requested_by=uuid4(),
            trace=None,
        )
    assert refused.value.code == code
    fake.finished()


def test_a_mapped_stale_index_answers_412_first() -> None:
    fake = ScriptedConnection(index_row(row_version=OTHER_ROW_VERSION))
    with pytest.raises(IngestionRefusedError) as refused:
        create_index_delete_run(
            connection_of(fake),
            index_id=uuid4(),
            row_version=ROW_VERSION,
            requested_by=uuid4(),
            trace=None,
        )
    assert refused.value.code == "precondition_failed"


# --- The logical index status (T-111) -------------------------------------------------------------


def facts(**changes: Any) -> IndexStatusFacts:
    values: dict[str, Any] = {
        "embedding_model": MODEL,
        "embedding_dimensions": 1024,
        "language_analyzer": ANALYZER,
        "active": PhysicalIndexSettings(MODEL, 1024, ANALYZER, 1),
        "stale_served_sets": False,
    }
    values.update(changes)
    return IndexStatusFacts(**values)


def test_t111_failed_with_no_active_physical_index() -> None:
    assert decide_index_status(facts(active=None)) is IndexStatus.FAILED


@pytest.mark.parametrize(
    "mismatch",
    [
        {"embedding_model": "text-embedding-3-small"},
        {"embedding_dimensions": 3072},
        {"language_analyzer": "de.lucene"},
        {"active": PhysicalIndexSettings(MODEL, 1024, ANALYZER, 2)},
        {"stale_served_sets": True},
    ],
)
def test_t111_reingest_required_for_each_mismatch(mismatch: dict[str, Any]) -> None:
    assert decide_index_status(facts(**mismatch)) is IndexStatus.REINGEST_REQUIRED


def test_t111_ready_otherwise() -> None:
    assert decide_index_status(facts()) is IndexStatus.READY


def status_row(*, active: UUID | None, analyzer: str = ANALYZER) -> Result:
    return rows(
        SimpleNamespace(
            method_key="recursive",
            method_version=1,
            method_parameter_hash=b"\x01" * 32,
            embedding_model=MODEL,
            embedding_dimensions=1024,
            language_analyzer=ANALYZER,
            active_id=active,
            active_model=None if active is None else MODEL,
            active_dimensions=None if active is None else 1024,
            active_analyzer=None if active is None else analyzer,
            active_schema_version=None if active is None else 1,
        )
    )


def test_compute_index_status_reads_the_index_and_its_served_sets() -> None:
    active = uuid4()
    fake = ScriptedConnection(status_row(active=active), rows())
    assert compute_index_status(connection_of(fake), uuid4()) is IndexStatus.READY
    assert "LEFT OUTER JOIN app.physical_indexes" in fake.executed[0].sql
    assert "state = N'active'" in fake.executed[0].sql
    stale_sql = fake.executed[1].sql
    assert "JOIN app.served_chunk_sets" in stale_sql
    assert "app.chunk_sets.parameter_hash != :" in stale_sql
    assert fake.executed[1].values["physical_index_id_1"] == active
    fake.finished()


def test_compute_index_status_finds_a_stale_set_or_setting() -> None:
    stale = ScriptedConnection(status_row(active=uuid4()), rows(SimpleNamespace(id=uuid4())))
    assert compute_index_status(connection_of(stale), uuid4()) is IndexStatus.REINGEST_REQUIRED
    moved = ScriptedConnection(status_row(active=uuid4(), analyzer="fr.lucene"), rows())
    assert compute_index_status(connection_of(moved), uuid4()) is IndexStatus.REINGEST_REQUIRED


def test_compute_index_status_without_an_active_index() -> None:
    fake = ScriptedConnection(status_row(active=None))
    assert compute_index_status(connection_of(fake), uuid4()) is IndexStatus.FAILED
    fake.finished()


def test_compute_index_status_of_a_missing_index() -> None:
    with pytest.raises(LookupError):
        compute_index_status(connection_of(ScriptedConnection(rows())), uuid4())


# --- Failure side effects (section 6.11) ----------------------------------------------------------


def test_the_failure_side_effects_retire_the_build_and_fail_the_version() -> None:
    item = uuid4()
    fake = ScriptedConnection(count(1), count(1))

    effects = apply_failure_side_effects(connection_of(fake), item, "embedding_failed")

    assert effects == FailureSideEffects(chunk_set_retired=True, version_failed=True)
    fake.finished()
    chunk_set, version = fake.executed
    assert chunk_set.sql.startswith(
        "UPDATE app.chunk_sets SET state=N'retired', retired_at=SYSUTCDATETIME() "
        "FROM app.chunk_sets, app.ingestion_run_items AS i "
    )
    assert "app.chunk_sets.id = i.chunk_set_id AND i.id = :id_1" in chunk_set.sql
    assert "app.chunk_sets.state IN (N'building', N'ready')" in chunk_set.sql
    assert chunk_set.values["id_1"] == item
    assert version.sql.startswith(
        "UPDATE app.document_versions SET state=N'failed', failure_code=:failure_code, "
        "failed_at=SYSUTCDATETIME() FROM app.document_versions, app.ingestion_run_items AS i "
    )
    assert "app.document_versions.id = i.document_version_id AND i.id = :id_1" in version.sql
    assert "app.document_versions.state IN (N'pending', N'ingesting')" in version.sql
    assert version.values["failure_code"] == "embedding_failed"
    assert version.values["id_1"] == item


def test_the_version_fails_only_for_the_active_or_else_the_building_index() -> None:
    fake = ScriptedConnection(count(0), count(0))

    effects = apply_failure_side_effects(connection_of(fake), uuid4(), "processing_interrupted")

    assert effects == FailureSideEffects(chunk_set_retired=False, version_failed=False)
    version = fake.executed[1].sql
    target = version[version.index("EXISTS") :]
    assert "app.search_indexes.id = i.search_index_id" in target
    assert "app.search_indexes.active_physical_index_id = i.physical_index_id OR " in target
    assert "app.search_indexes.active_physical_index_id IS NULL AND (EXISTS" in target
    building = target[target.index("FROM app.physical_indexes") :]
    # The building check reads the failed item, not a copy of the run-item table of its own.
    assert building.startswith("FROM app.physical_indexes \nWHERE ")
    assert "ingestion_run_items" not in building
    assert "app.physical_indexes.id = i.physical_index_id" in building
    assert "app.physical_indexes.search_index_id = i.search_index_id" in building
    assert "app.physical_indexes.state = N'building'" in building


def test_the_version_update_sets_only_the_operator_roles_columns() -> None:
    fake = ScriptedConnection(count(1), count(1))
    apply_failure_side_effects(connection_of(fake), uuid4(), "processing_interrupted")
    version = fake.executed[1].statement.compile(dialect=DIALECT)
    assignments = str(version).split(" SET ", 1)[1].split(" FROM ", 1)[0]
    assert [part.split("=")[0] for part in assignments.split(", ")] == [
        "state",
        "failure_code",
        "failed_at",
    ]
