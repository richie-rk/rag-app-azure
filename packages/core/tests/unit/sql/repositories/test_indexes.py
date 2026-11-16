"""Unit tests of the logical-index statements (`11-api.md` section 3.10; `14-ingestion.md` sections
7.2 and 8.3 to 8.7), on a scripted connection: every statement is compiled for SQL Server and
each read is answered from the script in order; inserts are answered without consuming it. The
ledger functions are replaced by recorders here, because their own statements are proven by
`packages/core/tests/unit/ingestion/test_runs.py`; the whole operations against SQL Server are
`apps/api/tests/integration/routes/test_admin_indexes.py`."""

from __future__ import annotations

import json
import re
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy.dialects import mssql
from sqlalchemy.sql.dml import Insert, Update
from sqlalchemy.sql.elements import ClauseElement

from docchat_core.ingestion.methods import RECURSIVE_V1, RecursiveParametersV1
from docchat_core.ingestion.models import (
    ContentType,
    IndexStatus,
    IngestionRefusedError,
    PhysicalIndexState,
    ReingestMode,
    RunKind,
    RunState,
    TraceContext,
)
from docchat_core.ingestion.runs import (
    IndexCreateResult,
    IndexDeleteResult,
    ReingestResult,
)
from docchat_core.ingestion.settings import DEFAULT_EMBEDDING_MODELS, EmbeddingModelCatalog
from docchat_core.sql.engine import REQUEST_PATH_REGISTRY
from docchat_core.sql.repositories import indexes
from docchat_core.sql.repositories.indexes import (
    IndexChanges,
    IndexFilters,
    IndexPosition,
    IndexSettingsRefusedError,
    NewIndex,
    StoredIndex,
)
from docchat_core.sql.tables.access import audit_events
from docchat_core.sql.tables.ingestion import search_indexes

DIALECT = mssql.dialect()  # type: ignore[no-untyped-call]
INDEX = UUID("8b3c4d5e-6f7a-4b9c-8d1e-2f3a4b5c6d62")
PHYSICAL = UUID("1b3c4d5e-6f7a-4b9c-8d1e-2f3a4b5c6d62")
BUILDING = UUID("2b3c4d5e-6f7a-4b9c-8d1e-2f3a4b5c6d62")
PROJECT = UUID("7a2b3c4d-5e6f-4a8b-9c0d-1e2f3a4b5c61")
ADMIN = UUID("0ca20100-0000-4000-8000-000000000003")
ROW_VERSION = b"\x00\x00\x00\x00\x00\x00\x07\xd1"
TRACE = TraceContext("00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01")
# The driver returns `DATETIME2` values without an offset.
AT = datetime(2026, 10, 21, 9, 15, 2, 123000, tzinfo=UTC).replace(tzinfo=None)
CATALOG = EmbeddingModelCatalog.model_construct(openai_embedding_models=DEFAULT_EMBEDDING_MODELS)
RECURSIVE_DEFAULTS = {"targetTokens": 512, "overlapPercent": 25, "tableMaxTokens": 1600}
MODEL = "text-embedding-3-large"


class Row:
    def __init__(self, **values: Any) -> None:
        self._mapping: dict[str, Any] = values

    def __getattr__(self, name: str) -> Any:
        try:
            return self._mapping[name]
        except KeyError:
            raise AttributeError(name) from None


@dataclass
class Result:
    rows: list[Any] = field(default_factory=list)
    rowcount: int = 1

    def all(self) -> list[Any]:
        return list(self.rows)

    def first(self) -> Any:
        return self.rows[0] if self.rows else None

    def one(self) -> Any:
        assert len(self.rows) == 1
        return self.rows[0]

    def scalar_one(self) -> Any:
        return self.one()

    def __iter__(self) -> Iterator[Any]:
        return iter(self.rows)


def sql(statement: ClauseElement) -> str:
    return re.sub(r"\s+", " ", str(statement.compile(dialect=DIALECT))).strip()


class Script:
    def __init__(self, *results: Result) -> None:
        self.script = deque(results)
        self.executed: list[tuple[ClauseElement, Any]] = []

    def execute(self, statement: ClauseElement, parameters: Any = None) -> Result:
        sql(statement)
        self.executed.append((statement, parameters))
        if isinstance(statement, Insert):
            return Result()
        assert self.script, f"no scripted answer for {sql(statement)}"
        return self.script.popleft()

    def audit_rows(self) -> list[dict[str, Any]]:
        return [
            parameters
            for statement, parameters in self.executed
            if isinstance(statement, Insert) and statement.table is audit_events
        ]

    def updates(self) -> list[dict[str, Any]]:
        return [
            dict(statement.compile(dialect=DIALECT).params)
            for statement, _ in self.executed
            if isinstance(statement, Update) and statement.table is search_indexes
        ]


@dataclass
class Engine:
    connection: Script
    calls: list[tuple[str, str]] = field(default_factory=list)

    async def run(self, work: Callable[[Any], Any], *, name: str, retry: str) -> Any:
        self.calls.append((name, retry))
        return work(self.connection)

    async def fetch_all(
        self, statement: ClauseElement, parameters: Any = None, *, name: str
    ) -> list[Any]:
        self.calls.append((name, "transient"))
        return self.connection.execute(statement, parameters).all()


def index_row(**overrides: Any) -> Row:
    values: dict[str, Any] = {
        "id": INDEX,
        "display_name": "Policy library",
        "description": "Shared policies",
        "status": "ready",
        "method_key": "recursive",
        "method_version": 1,
        "method_display_name": "By headings and paragraphs",
        "method_parameters": json.dumps(RECURSIVE_DEFAULTS),
        "embedding_model": MODEL,
        "embedding_dimensions": 1024,
        "language_analyzer": "standard.lucene",
        "created_at": AT,
        "created_by": ADMIN,
        "updated_at": AT,
        "updated_by": ADMIN,
        "row_version": ROW_VERSION,
        "live_document_count": 3,
    }
    return Row(**(values | overrides))


def physical_row(physical_id: UUID = PHYSICAL, **overrides: Any) -> Row:
    values: dict[str, Any] = {
        "id": physical_id,
        "generation": 1,
        "schema_version": 1,
        "embedding_model": MODEL,
        "embedding_dimensions": 1024,
        "language_analyzer": "standard.lucene",
        "state": "active",
        "created_at": AT,
        "activated_at": AT,
    }
    return Row(**(values | overrides))


def counts_row() -> Row:
    return Row(live=3, ingested=2, in_progress=1, failed=0)


def detail_script(**index: Any) -> list[Result]:
    """The reads of `read_index_detail` for an index that isn't rebuilding."""
    return [
        Result([index_row(**index)]),
        Result([physical_row()]),
        Result([counts_row()]),
        Result([Row(id=PROJECT, name="Field notes")]),
        Result([]),
    ]


def locked_row(**overrides: Any) -> Row:
    values: dict[str, Any] = {
        "status": "ready",
        "row_version": ROW_VERSION,
        "display_name": "Policy library",
        "description": None,
        "method_key": "recursive",
        "method_version": 1,
        "method_parameters": json.dumps(RECURSIVE_DEFAULTS),
        "embedding_model": MODEL,
        "embedding_dimensions": 1024,
        "language_analyzer": "standard.lucene",
    }
    return Row(**(values | overrides))


def methods(*rows: tuple[int, bool]) -> Result:
    return Result([Row(method_version=version, is_active=active) for version, active in rows])


@dataclass
class Recorder:
    result: Any
    calls: list[dict[str, Any]] = field(default_factory=list)

    def __call__(self, connection: Any, *args: Any, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self.result


# --- Statements -----------------------------------------------------------------------------


def test_every_list_shape_is_its_own_constant_text() -> None:
    texts = {sql(statement) for statement in indexes.INDEX_PAGES.values()}

    assert len(texts) == 8
    assert not any("IS NULL OR" in text for text in texts)
    assert all(text.endswith("ORDER BY s.display_name, s.id") for text in texts)


def test_without_a_status_the_list_keeps_the_filtered_indexs_literal() -> None:
    plain = sql(indexes.INDEX_PAGES[(False, True, True)])
    filtered = sql(indexes.INDEX_PAGES[(True, False, False)])

    assert (
        "WHERE s.status != N'deleted' AND s.display_name LIKE :prefix ESCAPE '\\' AND "
        "(s.display_name > :after_name OR s.display_name = :after_name AND "
        "s.id > :after_index_id)"
    ) in plain
    assert "WHERE s.status = :status ORDER BY" in filtered
    assert (
        "(SELECT count(*) AS count_1 FROM app.documents AS live_d WHERE live_d.search_index_id "
        "= s.id AND live_d.is_deleted = 0 AND live_d.current_version_id IS NOT NULL) AS "
        "live_document_count"
    ) in plain


def test_the_detail_reads_seek_their_indexes() -> None:
    assert "WITH (UPDLOCK, ROWLOCK)" in sql(indexes.INDEX_FOR_UPDATE)
    assert sql(indexes.OPEN_RUN).endswith(
        "WHERE app.ingestion_runs.search_index_id = :index_id AND app.ingestion_runs.state IN "
        "(N'queued', N'running') ORDER BY app.ingestion_runs.created_at DESC, "
        "app.ingestion_runs.id DESC"
    )
    assert "app.physical_indexes.state IN (N'active', N'building')" in sql(indexes.PHYSICAL_INDEXES)
    assert "ORDER BY app.projects.name" in sql(indexes.MAPPED_PROJECTS)
    assert "sc.physical_index_id = :building_id" in sql(indexes.SERVED_CURRENT)
    assert "N'in_progress'" in sql(indexes.DOCUMENT_COUNTS)


# --- Reads ----------------------------------------------------------------------------------


async def test_the_index_options_parse_the_catalogue_rows() -> None:
    script = Script(
        Result(
            [
                Row(
                    method_key="recursive",
                    method_version=1,
                    display_name="By headings and paragraphs",
                    description="Splits.",
                    parameter_schema=RECURSIVE_V1.parameter_schema,
                    default_parameters=json.dumps(RECURSIVE_DEFAULTS),
                    supported_content_types='["application/pdf"]',
                )
            ]
        )
    )

    (option,) = await indexes.read_index_options(Engine(script))  # type: ignore[arg-type]

    assert script.executed[0][0] is indexes.ACTIVE_METHODS
    assert option.default_parameters == RECURSIVE_DEFAULTS
    assert option.parameter_schema["required"] == [
        "targetTokens",
        "overlapPercent",
        "tableMaxTokens",
    ]
    assert option.supported_content_types == (ContentType.PDF,)


async def test_read_indexes_binds_the_filters() -> None:
    script = Script(Result([index_row()]))
    after = IndexPosition(display_name="A", index_id=uuid4())

    (summary,) = await indexes.read_indexes(
        Engine(script),  # type: ignore[arg-type]
        filters=IndexFilters(status=IndexStatus.READY, name_prefix="pol_"),
        after=after,
        limit_plus_one=51,
    )

    statement, parameters = script.executed[0]
    assert statement is indexes.INDEX_PAGES[(True, True, True)]
    assert parameters == {
        "limit_plus_one": 51,
        "status": "ready",
        "prefix": "pol\\_%",
        "after_name": "A",
        "after_index_id": after.index_id,
    }
    assert summary.status is IndexStatus.READY
    assert summary.live_document_count == 3
    assert summary.updated_at == AT.replace(tzinfo=UTC)


@pytest.mark.parametrize("rows", [[], [index_row(status="deleted")]])
def test_a_missing_or_deleted_index_has_no_detail(rows: list[Row]) -> None:
    script = Script(Result(rows))

    assert indexes.read_index_detail(script, INDEX) is None  # type: ignore[arg-type]


def test_the_detail_of_a_ready_index() -> None:
    open_run = Row(id=uuid4(), kind="upload", state="queued")
    script = Script(*detail_script()[:-1], Result([open_run]))

    detail = indexes.read_index_detail(script, INDEX)  # type: ignore[arg-type]

    assert detail is not None
    assert detail.method_parameters == RECURSIVE_DEFAULTS
    assert detail.active is not None
    assert detail.active.state is PhysicalIndexState.ACTIVE
    assert detail.building is None
    assert detail.rebuild_progress is None
    assert detail.document_counts == indexes.DocumentCounts(3, 2, 1, 0)
    assert detail.mapped_projects == (indexes.ProjectRefRow(PROJECT, "Field notes"),)
    assert detail.open_run == indexes.OpenRunRow(open_run.id, RunKind.UPLOAD, RunState.QUEUED)


def test_a_rebuilding_index_reports_its_rebuild_progress() -> None:
    script = Script(
        Result([index_row(status="rebuilding")]),
        Result([physical_row(), physical_row(BUILDING, generation=2, state="building")]),
        Result([counts_row()]),
        Result([2]),
        Result([]),
        Result([]),
    )

    detail = indexes.read_index_detail(script, INDEX)  # type: ignore[arg-type]

    assert detail is not None
    assert detail.building is not None
    assert detail.building.generation == 2
    assert detail.rebuild_progress == indexes.RebuildProgress(served_current=2, live_documents=3)
    assert script.executed[3] == (
        indexes.SERVED_CURRENT,
        {"index_id": INDEX, "building_id": BUILDING},
    )


async def test_read_index_runs_the_detail_in_one_unit_of_work() -> None:
    engine = Engine(Script(*detail_script()))

    detail = await indexes.read_index(engine, index_id=INDEX)  # type: ignore[arg-type]

    assert detail is not None
    assert engine.calls == [(indexes.READ_INDEX, "transient")]


# --- The catalogue checks -------------------------------------------------------------------


def _new(**overrides: Any) -> NewIndex:
    values: dict[str, Any] = {
        "display_name": "Policy library",
        "description": None,
        "method_key": "recursive",
        "method_version": None,
        "method_parameters": None,
        "embedding_model": MODEL,
        "embedding_dimensions": None,
        "language_analyzer": None,
    }
    return NewIndex(**(values | overrides))


def test_defaults_fill_every_unnamed_setting() -> None:
    script = Script(methods((1, True)))

    settings = indexes.resolve_new_settings(script, _new(), CATALOG)  # type: ignore[arg-type]

    assert settings.method is RECURSIVE_V1
    assert settings.parameters == RecursiveParametersV1()
    assert (settings.dimensions, settings.analyzer) == (1024, "standard.lucene")


@pytest.mark.parametrize(
    ("rows", "request_overrides", "code"),
    [
        ((), {}, "method_not_found"),
        (((1, False),), {}, "method_inactive"),
        (((1, True),), {"method_version": 2}, "method_not_found"),
        (((1, True),), {"embedding_model": "ada"}, "unsupported_embedding_model"),
        (((1, True),), {"embedding_dimensions": 512}, "unsupported_dimensions"),
        (((1, True),), {"language_analyzer": "xx.lucene"}, "unsupported_analyzer"),
    ],
)
def test_each_catalogue_refusal(
    rows: tuple[tuple[int, bool], ...], request_overrides: dict[str, Any], code: str
) -> None:
    script = Script(methods(*rows))

    with pytest.raises(IndexSettingsRefusedError) as refused:
        indexes.resolve_new_settings(script, _new(**request_overrides), CATALOG)  # type: ignore[arg-type]

    assert refused.value.code == code
    assert refused.value.parameter_errors == ()


def test_invalid_parameters_carry_the_models_errors_without_their_input() -> None:
    script = Script(methods((1, True)))
    request = _new(method_parameters={"targetTokens": 99, "overlapPercent": 27, "extra": 1})

    with pytest.raises(IndexSettingsRefusedError) as refused:
        indexes.resolve_new_settings(script, request, CATALOG)  # type: ignore[arg-type]

    assert refused.value.code == "invalid_method_parameters"
    errors = refused.value.parameter_errors
    assert [(error["loc"], error["type"]) for error in errors] == [
        (("targetTokens",), "greater_than_equal"),
        (("overlapPercent",), "multiple_of"),
        (("extra",), "extra_forbidden"),
    ]
    assert all("input" not in error for error in errors)


# --- Create ---------------------------------------------------------------------------------


async def test_create_runs_the_ledger_function_audits_and_reads_the_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = uuid4()
    recorder = Recorder(IndexCreateResult(index_id=INDEX, physical_index_id=PHYSICAL, run_id=run))
    monkeypatch.setattr(indexes, "create_index_create_run", recorder)
    script = Script(methods((1, True)), *detail_script(status="provisioning"))
    engine = Engine(script)

    created = await indexes.create_index(
        engine,  # type: ignore[arg-type]
        request=_new(description="Shared"),
        catalog=CATALOG,
        max_indexes=15,
        created_by=ADMIN,
        trace=TRACE,
    )

    assert engine.calls == [(indexes.CREATE_INDEX, "none")]
    (call,) = recorder.calls
    assert (call["display_name"], call["description"], call["max_indexes"]) == (
        "Policy library",
        "Shared",
        15,
    )
    assert (call["created_by"], call["trace"]) == (ADMIN, TRACE)
    assert created.run_id == run
    assert created.index.summary.status is IndexStatus.PROVISIONING
    (audit,) = script.audit_rows()
    assert (audit["action"], audit["target_type"], audit["target_id"]) == (
        "index.created",
        "search_index",
        str(INDEX),
    )
    assert json.loads(audit["metadata_json"]) == {
        "methodKey": "recursive",
        "methodVersion": 1,
        "embeddingModel": MODEL,
        "embeddingDimensions": 1024,
        "languageAnalyzer": "standard.lucene",
        "physicalIndexId": str(PHYSICAL),
        "runId": str(run),
    }


# --- Update ---------------------------------------------------------------------------------


def _stored(**overrides: Any) -> StoredIndex:
    values: dict[str, Any] = {
        "status": IndexStatus.READY,
        "row_version": ROW_VERSION,
        "display_name": "Policy library",
        "description": None,
        "method_key": "recursive",
        "method_version": 1,
        "method_parameters": RECURSIVE_DEFAULTS,
        "embedding_model": MODEL,
        "embedding_dimensions": 1024,
        "language_analyzer": "standard.lucene",
    }
    return StoredIndex(**(values | overrides))


def test_a_new_method_key_takes_its_highest_active_version_and_defaults() -> None:
    script = Script(methods((1, True)))
    changes = IndexChanges(fields=frozenset({"methodKey"}), method_key="pagewise")

    target = indexes.resolve_target(script, _stored(), changes, CATALOG)  # type: ignore[arg-type]

    assert (target.method_key, target.method_version) == ("pagewise", 1)
    assert target.method_parameters == {"summarizeTables": True, "ocrLanguages": ["eng"]}
    assert target.method_parameter_hash is not None
    assert indexes.changed_fields(_stored(), target) == ("methodKey", "methodParameters")


def test_parameters_alone_are_validated_against_the_indexs_method() -> None:
    script = Script(methods((1, True)))
    changes = IndexChanges(
        fields=frozenset({"methodParameters"}), method_parameters={"targetTokens": 1024}
    )

    target = indexes.resolve_target(script, _stored(), changes, CATALOG)  # type: ignore[arg-type]

    assert target.method_parameters == RECURSIVE_DEFAULTS | {"targetTokens": 1024}
    assert indexes.changed_fields(_stored(), target) == ("methodParameters",)


def test_untouched_settings_are_not_checked_against_the_catalogue() -> None:
    script = Script()
    changes = IndexChanges(
        fields=frozenset({"displayName", "description"}), display_name="New", description=None
    )

    target = indexes.resolve_target(script, _stored(), changes, CATALOG)  # type: ignore[arg-type]

    assert script.executed == []
    assert target.method_parameter_hash is None
    assert indexes.changed_fields(_stored(), target) == ("displayName",)


def test_a_new_model_without_dimensions_takes_its_default_and_an_analyzer_is_checked() -> None:
    changes = IndexChanges(
        fields=frozenset({"embeddingDimensions", "languageAnalyzer"}),
        embedding_dimensions=3072,
        language_analyzer="pt-Br.microsoft",
    )

    target = indexes.resolve_target(Script(), _stored(), changes, CATALOG)  # type: ignore[arg-type]

    assert (target.embedding_dimensions, target.language_analyzer) == (3072, "pt-Br.microsoft")
    assert indexes.changed_fields(_stored(), target) == ("embeddingDimensions", "languageAnalyzer")


async def _update(script: Script, changes: IndexChanges, seen: list[bytes] | None = None) -> Any:
    return await indexes.update_index(
        Engine(script),  # type: ignore[arg-type]
        index_id=INDEX,
        changes=changes,
        check_version=(seen if seen is not None else []).append,
        catalog=CATALOG,
        updated_by=ADMIN,
    )


async def test_an_unchanged_body_writes_nothing() -> None:
    script = Script(Result([locked_row()]), *detail_script())

    updated = await _update(
        script, IndexChanges(fields=frozenset({"displayName"}), display_name="Policy library")
    )

    assert updated.events == ()
    assert script.updates() == []
    assert script.audit_rows() == []


async def test_a_missing_index_answers_before_the_version_check() -> None:
    seen: list[bytes] = []

    with pytest.raises(IngestionRefusedError) as refused:
        await _update(
            Script(Result([])),
            IndexChanges(fields=frozenset({"displayName"}), display_name="X"),
            seen,
        )

    assert refused.value.code == "index_not_found"
    assert seen == []


@pytest.mark.parametrize(
    ("status", "changes"),
    [
        (
            "reingesting",
            IndexChanges(fields=frozenset({"embeddingDimensions"}), embedding_dimensions=256),
        ),
        (
            "provisioning",
            IndexChanges(fields=frozenset({"languageAnalyzer"}), language_analyzer="en.lucene"),
        ),
        ("deleting", IndexChanges(fields=frozenset({"displayName"}), display_name="X")),
    ],
)
async def test_a_change_the_status_doesnt_allow_is_index_busy(
    status: str, changes: IndexChanges
) -> None:
    script = Script(Result([locked_row(status=status)]))
    seen: list[bytes] = []

    with pytest.raises(IngestionRefusedError) as refused:
        await _update(script, changes, seen)

    assert refused.value.code == "index_busy"
    assert seen == [ROW_VERSION]


async def test_a_name_changes_while_reingesting() -> None:
    script = Script(Result([locked_row(status="reingesting")]), Result(), *detail_script())

    updated = await _update(
        script, IndexChanges(fields=frozenset({"displayName"}), display_name="Renamed")
    )

    (update,) = script.updates()
    assert update["display_name"] == "Renamed"
    assert update["row_version_1"] == ROW_VERSION
    assert json.loads(script.audit_rows()[0]["metadata_json"]) == {
        "changedFields": ["displayName"],
        "status": "reingesting",
    }
    assert updated.events[0].action == "index.updated"


async def test_a_settings_change_sets_the_status_the_settings_call_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        indexes, "compute_index_status", lambda connection, index_id: IndexStatus.REINGEST_REQUIRED
    )
    script = Script(
        Result([locked_row()]), methods((1, True)), Result(), Result(), *detail_script()
    )

    await _update(
        script,
        IndexChanges(
            fields=frozenset({"methodParameters"}), method_parameters={"targetTokens": 256}
        ),
    )

    update, status = script.updates()
    assert update["method_key"] == "recursive"
    assert json.loads(update["method_parameters"])["targetTokens"] == 256
    assert status["status"] == "reingest_required"
    assert json.loads(script.audit_rows()[0]["metadata_json"]) == {
        "changedFields": ["methodParameters"],
        "status": "reingest_required",
    }


async def test_a_failed_index_keeps_its_status(monkeypatch: pytest.MonkeyPatch) -> None:
    def never(connection: Any, index_id: UUID) -> IndexStatus:
        raise AssertionError("a failed index keeps its status")

    monkeypatch.setattr(indexes, "compute_index_status", never)
    script = Script(Result([locked_row(status="failed")]), Result(), *detail_script())

    await _update(
        script, IndexChanges(fields=frozenset({"embeddingDimensions"}), embedding_dimensions=256)
    )

    (update,) = script.updates()
    assert update["embedding_dimensions"] == 256


async def test_a_guarded_update_that_changes_nothing_is_precondition_failed() -> None:
    script = Script(Result([locked_row()]), Result(rowcount=0), Result([locked_row()]))

    with pytest.raises(IngestionRefusedError) as refused:
        await _update(script, IndexChanges(fields=frozenset({"displayName"}), display_name="X"))

    assert refused.value.code == "precondition_failed"


# --- Delete and re-ingest -------------------------------------------------------------------


async def test_delete_counts_its_items_and_audits(monkeypatch: pytest.MonkeyPatch) -> None:
    run = uuid4()
    recorder = Recorder(IndexDeleteResult(run, physical_index_count=2, document_count=5))
    monkeypatch.setattr(indexes, "create_index_delete_run", recorder)
    script = Script(Result([locked_row()]), *detail_script(status="deleting"))
    engine = Engine(script)
    seen: list[bytes] = []

    deleted = await indexes.delete_index(
        engine,  # type: ignore[arg-type]
        index_id=INDEX,
        check_version=seen.append,
        requested_by=ADMIN,
        trace=TRACE,
    )

    assert engine.calls == [(indexes.DELETE_INDEX, "none")]
    assert seen == [ROW_VERSION]
    assert recorder.calls[0]["row_version"] == ROW_VERSION
    assert (deleted.run_id, deleted.item_count) == (run, 3)
    assert json.loads(script.audit_rows()[0]["metadata_json"]) == {
        "runId": str(run),
        "physicalIndexCount": 2,
        "documentCount": 5,
    }


async def test_reingest_audits_its_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    run = uuid4()
    recorder = Recorder(
        ReingestResult(run, ReingestMode.NEW_PHYSICAL_INDEX, 4, BUILDING, IndexStatus.REBUILDING)
    )
    monkeypatch.setattr(indexes, "create_reingest_run", recorder)
    script = Script(
        Result([locked_row(status="reingest_required")]),
        Result([index_row(status="rebuilding")]),
        Result([physical_row()]),
        Result([counts_row()]),
        Result([]),
        Result([]),
    )

    started = await indexes.start_reingest(
        Engine(script),  # type: ignore[arg-type]
        index_id=INDEX,
        check_version=accept,
        requested_by=ADMIN,
        trace=TRACE,
        max_indexes=15,
        catalog=CATALOG,
    )

    assert (recorder.calls[0]["max_indexes"], recorder.calls[0]["catalog"]) == (15, CATALOG)
    assert (started.mode, started.item_count) == (ReingestMode.NEW_PHYSICAL_INDEX, 4)
    assert json.loads(script.audit_rows()[0]["metadata_json"]) == {
        "runId": str(run),
        "mode": "new_physical_index",
        "itemCount": 4,
    }


def accept(_row_version: bytes) -> None:
    return


def test_every_function_is_registered_for_the_plan_audit() -> None:
    for name in (
        "read_index_options",
        "read_indexes",
        "read_index",
        "create_index",
        "update_index",
        "delete_index",
        "start_reingest",
    ):
        entry = REQUEST_PATH_REGISTRY[f"docchat_core.sql.repositories.indexes.{name}"]
        assert entry.function is getattr(indexes, name)
