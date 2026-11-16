"""Unit tests of the document statements (`11-api.md` sections 3.11.1 to 3.11.4; `14-ingestion.md`
sections 7.1 and 8.11), on a scripted connection: every statement is compiled for SQL Server and
each read is answered from the script in order; inserts are answered without consuming it. The
ledger functions are replaced by recorders here, because their own statements are proven by
`packages/core/tests/unit/ingestion/test_runs.py`; the whole operations against SQL Server are
`apps/api/tests/integration/routes/test_admin_documents.py`."""

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
from sqlalchemy.sql.dml import Insert
from sqlalchemy.sql.elements import ClauseElement

from docchat_core.ingestion.models import (
    ContentType,
    DocumentPdfStatus,
    DocumentVersionState,
    IngestionRefusedError,
    ScanStatus,
    TraceContext,
)
from docchat_core.ingestion.runs import DocumentDeleteResult, DocumentReingestResult
from docchat_core.sql.engine import REQUEST_PATH_REGISTRY
from docchat_core.sql.repositories import documents
from docchat_core.sql.repositories.documents import DocumentFilters, DocumentPosition
from docchat_core.sql.tables.access import audit_events

DIALECT = mssql.dialect()  # type: ignore[no-untyped-call]
INDEX = UUID("8b3c4d5e-6f7a-4b9c-8d1e-2f3a4b5c6d62")
DOCUMENT = UUID("ad5e6f7a-8b9c-4dae-9f20-4b5c6d7e8f64")
PROJECT = UUID("7a2b3c4d-5e6f-4a8b-9c0d-1e2f3a4b5c61")
ADMIN = UUID("0ca20100-0000-4000-8000-000000000003")
ROW_VERSION = b"\x00\x00\x00\x00\x00\x00\x07\xd1"
TRACE = TraceContext("00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01")
# The driver returns `DATETIME2` values without an offset.
AT = datetime(2026, 10, 21, 9, 15, 2, 123000, tzinfo=UTC).replace(tzinfo=None)


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

    def all(self) -> list[Any]:
        return list(self.rows)

    def first(self) -> Any:
        return self.rows[0] if self.rows else None

    def scalar_one_or_none(self) -> Any:
        return self.rows[0] if self.rows else None

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


@dataclass
class Engine:
    connection: Script
    calls: list[tuple[str, str]] = field(default_factory=list)

    async def run(self, work: Callable[[Any], Any], *, name: str, retry: str) -> Any:
        self.calls.append((name, retry))
        return work(self.connection)


def version_values(prefix: str, **overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "id": uuid4(),
        "version_number": 1,
        "state": "ingested",
        "size_bytes": 482113,
        "sha256": bytes(range(32)),
        "content_type": "application/pdf",
        "scan_status": "not_required",
        "failure_code": None,
        "uploaded_by": ADMIN,
        "uploaded_via_project_id": PROJECT,
        "reserved_at": AT,
        "committed_at": AT,
        "ingested_at": AT,
        "document_pdf_status": "built",
        "uploaded_by_name": "Carol",
        "project_name": "Field notes",
    } | overrides
    return {prefix + name: value for name, value in values.items()}


def document_values(*, latest: dict[str, Any] | None = None, **overrides: Any) -> Row:
    current = version_values("cv_")
    values: dict[str, Any] = {
        "id": DOCUMENT,
        "source_name": "Travel Policy.pdf",
        "normalized_name": "travel policy.pdf",
        "updated_at": AT,
        "row_version": ROW_VERSION,
        "display_state": "ingested",
        **current,
        **(latest if latest is not None else version_values("lv_", id=current["cv_id"])),
    }
    return Row(**(values | overrides))


def no_version(prefix: str) -> dict[str, Any]:
    return dict.fromkeys(version_values(prefix))


# --- Statements -----------------------------------------------------------------------------


def test_every_list_shape_is_its_own_constant_text() -> None:
    texts = {sql(statement) for statement in documents.DOCUMENT_PAGES.values()}

    assert len(texts) == 8
    assert not any("IS NULL OR" in text for text in texts)
    assert all(text.endswith("ORDER BY d.normalized_name, d.id") for text in texts)


def test_the_page_joins_the_current_and_the_latest_version() -> None:
    text = sql(documents.DOCUMENT_PAGES[(True, True, True)])

    assert "LEFT OUTER JOIN app.document_versions AS cv ON cv.id = d.current_version_id" in text
    assert (
        "LEFT OUTER JOIN app.document_versions AS lv ON lv.id = (SELECT TOP "
        "__[POSTCOMPILE_param_1] lv_pick.id FROM app.document_versions AS lv_pick WHERE "
        "lv_pick.document_id = d.id ORDER BY lv_pick.version_number DESC)"
    ) in text
    assert "LEFT OUTER JOIN app.users AS cu ON cu.object_id = cv.uploaded_by" in text
    assert "WHERE d.search_index_id = :index_id AND d.is_deleted = 0 AND CASE" in text
    assert "END = :display_state AND d.normalized_name LIKE :prefix ESCAPE '\\'" in text
    assert (
        "(d.normalized_name > :after_name OR d.normalized_name = :after_name AND "
        "d.id > :after_document_id)"
    ) in text


def test_the_display_state_follows_its_order() -> None:
    text = sql(documents.DOCUMENT_BY_ID)

    assert (
        "CASE WHEN ((EXISTS (SELECT * FROM app.document_versions AS open_v WHERE "
        "open_v.document_id = d.id AND open_v.state IN (N'uploading', N'pending', N'ingesting'))) "
        "OR (EXISTS (SELECT * FROM app.ingestion_run_items AS open_i WHERE open_i.document_id = "
        "d.id AND open_i.state IN (N'queued', N'running')))) THEN N'in_progress' WHEN "
        "(lv.state = N'failed') THEN N'failed' WHEN (d.current_version_id IS NOT NULL) THEN "
        "N'ingested' ELSE N'no_content' END AS display_state"
    ) in text
    assert text.endswith(
        "WHERE d.id = :document_id AND d.search_index_id = :index_id AND d.is_deleted = 0"
    )


def test_the_locked_read_names_the_live_document_of_the_index() -> None:
    assert sql(documents.LIVE_DOCUMENT_FOR_UPDATE) == (
        "SELECT app.documents.row_version FROM app.documents WITH (UPDLOCK, ROWLOCK) "
        "WHERE app.documents.id = :document_id AND app.documents.search_index_id = :index_id "
        "AND app.documents.is_deleted = 0"
    )


# --- Rows -----------------------------------------------------------------------------------


def test_a_row_without_the_version_reads_as_none() -> None:
    row = document_values(latest=no_version("lv_"), **no_version("cv_"), display_state="no_content")

    document = documents.document_row(row)  # type: ignore[arg-type]

    assert document.current_version is None
    assert document.latest_version is None
    assert document.display_state == "no_content"
    assert document.updated_at == AT.replace(tzinfo=UTC)


def test_a_version_maps_every_column() -> None:
    row = Row(**version_values("", failure_code="malware_detected", document_pdf_status=None))

    version = documents.version_row(row)  # type: ignore[arg-type]

    assert version is not None
    assert version.state is DocumentVersionState.INGESTED
    assert version.content_type is ContentType.PDF
    assert version.scan_status is ScanStatus.NOT_REQUIRED
    assert version.document_pdf_status is None
    assert version.sha256 == bytes(range(32))
    assert version.project_name == "Field notes"
    assert version.committed_at == AT.replace(tzinfo=UTC)


# --- Reads ----------------------------------------------------------------------------------


@pytest.mark.parametrize("status", [None, "deleted"])
async def test_a_missing_or_deleted_index_is_index_not_found(status: str | None) -> None:
    script = Script(Result([] if status is None else [status]))

    with pytest.raises(IngestionRefusedError) as refused:
        await documents.read_documents(
            Engine(script),  # type: ignore[arg-type]
            index_id=INDEX,
            filters=DocumentFilters(),
            after=None,
            limit_plus_one=51,
        )

    assert refused.value.code == "index_not_found"
    assert len(script.executed) == 1


async def test_read_documents_binds_the_filters_and_the_escaped_prefix() -> None:
    script = Script(Result(["ready"]), Result([document_values()]))
    engine = Engine(script)
    after = DocumentPosition(normalized_name="a", document_id=uuid4())

    (row,) = await documents.read_documents(
        engine,  # type: ignore[arg-type]
        index_id=INDEX,
        filters=DocumentFilters(display_state="failed", name_prefix="50%_off [draft]"),
        after=after,
        limit_plus_one=11,
    )

    assert engine.calls == [(documents.LIST_DOCUMENTS, "transient")]
    statement, parameters = script.executed[1]
    assert statement is documents.DOCUMENT_PAGES[(True, True, True)]
    assert parameters == {
        "index_id": INDEX,
        "limit_plus_one": 11,
        "display_state": "failed",
        "prefix": "50\\%\\_off \\[draft]%",
        "after_name": "a",
        "after_document_id": after.document_id,
    }
    assert row.current_version is not None
    assert row.latest_version is not None
    assert row.latest_version.document_pdf_status is DocumentPdfStatus.BUILT


async def test_read_document_is_document_not_found_for_another_indexs_document() -> None:
    script = Script(Result(["ready"]), Result([]))

    with pytest.raises(IngestionRefusedError) as refused:
        await documents.read_document(
            Engine(script),  # type: ignore[arg-type]
            index_id=INDEX,
            document_id=DOCUMENT,
        )

    assert refused.value.code == "document_not_found"


async def test_read_document_adds_its_versions_and_recent_items() -> None:
    versions = [Row(**version_values("", version_number=2)), Row(**version_values(""))]
    script = Script(Result(["ready"]), Result([document_values()]), Result(versions), Result([]))

    detail = await documents.read_document(
        Engine(script),  # type: ignore[arg-type]
        index_id=INDEX,
        document_id=DOCUMENT,
    )

    assert [version.version_number for version in detail.versions] == [2, 1]
    assert detail.recent_items == ()
    assert script.executed[2] == (documents.DOCUMENT_VERSIONS, {"document_id": DOCUMENT})


# --- Writes ---------------------------------------------------------------------------------


@dataclass
class Recorder:
    result: Any
    calls: list[dict[str, Any]] = field(default_factory=list)

    def __call__(self, connection: Any, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self.result


def checker(seen: list[bytes]) -> Callable[[bytes], None]:
    return seen.append


async def test_delete_checks_the_version_after_the_lookups_and_writes_its_audit_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = uuid4()
    recorder = Recorder(
        DocumentDeleteResult(run, superseded_version_count=2, cancelled_item_count=1)
    )
    monkeypatch.setattr(documents, "create_document_delete_run", recorder)
    script = Script(Result(["ready"]), Result([ROW_VERSION]))
    engine = Engine(script)
    seen: list[bytes] = []

    deleted = await documents.delete_document(
        engine,  # type: ignore[arg-type]
        index_id=INDEX,
        document_id=DOCUMENT,
        check_version=checker(seen),
        requested_by=ADMIN,
        trace=TRACE,
    )

    assert engine.calls == [(documents.DELETE_DOCUMENT, "none")]
    assert seen == [ROW_VERSION]
    assert recorder.calls == [
        {
            "index_id": INDEX,
            "document_id": DOCUMENT,
            "row_version": ROW_VERSION,
            "requested_by": ADMIN,
            "trace": TRACE,
        }
    ]
    assert deleted.run_id == run
    (audit,) = script.audit_rows()
    assert audit["action"] == "document.deleted"
    assert audit["target_type"] == "document"
    assert audit["target_id"] == str(DOCUMENT)
    assert json.loads(audit["metadata_json"]) == {
        "indexId": str(INDEX),
        "runId": str(run),
        "supersededVersionCount": 2,
        "cancelledItemCount": 1,
    }
    assert deleted.events[0].action == "document.deleted"


async def test_a_missing_document_answers_before_the_version_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(documents, "create_document_delete_run", Recorder(None))
    script = Script(Result(["ready"]), Result([]))
    seen: list[bytes] = []

    with pytest.raises(IngestionRefusedError) as refused:
        await documents.delete_document(
            Engine(script),  # type: ignore[arg-type]
            index_id=INDEX,
            document_id=DOCUMENT,
            check_version=checker(seen),
            requested_by=ADMIN,
            trace=None,
        )

    assert refused.value.code == "document_not_found"
    assert seen == []


async def test_a_failed_version_check_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = Recorder(None)
    monkeypatch.setattr(documents, "create_document_reingest_run", recorder)
    script = Script(Result(["ready"]), Result([ROW_VERSION]))

    def refuse(_: bytes) -> None:
        raise LookupError("stale")

    with pytest.raises(LookupError):
        await documents.reingest_document(
            Engine(script),  # type: ignore[arg-type]
            index_id=INDEX,
            document_id=DOCUMENT,
            check_version=refuse,
            requested_by=ADMIN,
            trace=None,
        )

    assert recorder.calls == []
    assert script.audit_rows() == []


@pytest.mark.parametrize(("created", "item_count"), [(True, 1), (False, 0)])
async def test_reingest_audits_the_run_only_when_it_created_one(
    monkeypatch: pytest.MonkeyPatch, created: bool, item_count: int
) -> None:
    run = uuid4()
    monkeypatch.setattr(
        documents,
        "create_document_reingest_run",
        Recorder(DocumentReingestResult(run, created=created, item_count=item_count)),
    )
    script = Script(Result(["ready"]), Result([ROW_VERSION]), Result([document_values()]))
    engine = Engine(script)

    reingested = await documents.reingest_document(
        engine,  # type: ignore[arg-type]
        index_id=INDEX,
        document_id=DOCUMENT,
        check_version=checker([]),
        requested_by=ADMIN,
        trace=TRACE,
    )

    assert engine.calls == [(documents.REINGEST_DOCUMENT, "none")]
    assert (reingested.run_id, reingested.created, reingested.item_count) == (
        run,
        created,
        item_count,
    )
    assert reingested.document.id == DOCUMENT
    (audit,) = script.audit_rows()
    assert json.loads(audit["metadata_json"]) == {
        "indexId": str(INDEX),
        "runId": str(run) if created else None,
        "itemCount": item_count,
    }


def test_every_function_is_registered_for_the_plan_audit() -> None:
    for name in ("read_documents", "read_document", "delete_document", "reingest_document"):
        entry = REQUEST_PATH_REGISTRY[f"docchat_core.sql.repositories.documents.{name}"]
        assert entry.function is getattr(documents, name)
    write = REQUEST_PATH_REGISTRY["docchat_core.sql.repositories.documents.delete_document"]
    assert write.example_params["check_version"] is documents.accept_any_version
    documents.accept_any_version(ROW_VERSION)
