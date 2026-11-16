"""Unit tests of the upload operations' statements (`14-ingestion.md` sections 2.1 to 2.6 and
5.5; `80-data-model.md` sections 2.12, 2.13 and 2.19), on a data-access layer without SQL.

The units of work run on a scripted connection: every statement is compiled for SQL Server, so a
statement the dialect can't render fails here, and each read or write is answered from the
script in order. The behaviour against SQL Server is proven by the upload integration tests
(`apps/api/tests/integration/routes/test_admin_uploads.py`).
"""

from __future__ import annotations

import inspect
import json
import re
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from opentelemetry.sdk.trace import TracerProvider
from sqlalchemy import Connection, Insert, create_engine
from sqlalchemy.sql.elements import ClauseElement

from docchat_core.ingestion.models import (
    ContentType,
    DocumentPdfStatus,
    DocumentVersionState,
    IndexStatus,
    ScanStatus,
    SubmitOutcome,
    TraceContext,
)
from docchat_core.ingestion.names import DocumentName
from docchat_core.ingestion.runs import UploadRunResult
from docchat_core.sql.engine import REQUEST_PATH_REGISTRY, SqlUniqueViolation
from docchat_core.sql.repositories import uploads
from docchat_core.sql.repositories.uploads import (
    CommitResult,
    PreflightMatch,
    Reservation,
    SubmittedUploads,
    UploadGate,
    UploadRefusedError,
    UploadVersion,
)
from docchat_core.sql.tables.access import audit_events

DIALECT = create_engine("mssql+pyodbc://").dialect
INDEX = UUID("4d8f0a71-2b3c-4d5e-8f60-718293a4b5c6")
PROJECT = UUID("5e9a1b82-3c4d-4e6f-9a71-8293a4b5c6d7")
DOCUMENT = UUID("6fab2c93-4d5e-4f70-8b82-93a4b5c6d7e8")
VERSION = UUID("7abc3da4-5e6f-4081-9c93-a4b5c6d7e8f9")
OID = UUID("0a11ce00-0000-4000-8000-000000000001")
SHA = bytes.fromhex("73b42435e5b4f2d1236c0444208788574db695cc1d77f6a9a7899020f3d44f06")
OTHER_SHA = bytes(32)
NAME = DocumentName("Travel Policy.pdf", "travel policy.pdf", ContentType.PDF)
TRACE = TraceContext("00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01")
NOW = datetime(2026, 10, 31, 9, 15, 2, 123000)  # noqa: DTZ001 - a DATETIME2 value as read


def sql(statement: ClauseElement) -> str:
    return re.sub(r"\s+", " ", str(statement.compile(dialect=DIALECT))).strip()


@dataclass
class Result:
    rows: list[Any] = field(default_factory=list)
    rowcount: int = 0

    def first(self) -> Any:
        return self.rows[0] if self.rows else None

    def all(self) -> list[Any]:
        return list(self.rows)

    def scalar_one(self) -> Any:
        assert len(self.rows) == 1
        return self.rows[0]

    def scalar_one_or_none(self) -> Any:
        return self.first()

    def scalars(self) -> list[Any]:
        return list(self.rows)


@dataclass
class Executed:
    sql: str
    statement: ClauseElement
    parameters: Any


class ScriptedConnection:
    """Answers each statement from the script in order; inserts are recorded and answered
    without consuming it."""

    def __init__(self, *results: Result) -> None:
        self.script = deque(results)
        self.executed: list[Executed] = []

    def execute(self, statement: ClauseElement, parameters: Any = None) -> Result:
        self.executed.append(Executed(sql(statement), statement, parameters))
        if isinstance(statement, Insert):
            return Result(rowcount=len(parameters) if isinstance(parameters, list) else 1)
        assert self.script, f"no scripted answer for: {sql(statement)}"
        return self.script.popleft()

    def statements(self) -> list[ClauseElement]:
        return [entry.statement for entry in self.executed]

    def finished(self) -> None:
        assert not self.script, "the script has unused answers"


def connection_of(fake: ScriptedConnection) -> Connection:
    return cast("Connection", fake)


def rows(*values: Any) -> Result:
    return Result(rows=list(values))


def count(value: int) -> Result:
    return Result(rowcount=value)


def version_row(**changes: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "id": VERSION,
        "document_id": DOCUMENT,
        "search_index_id": INDEX,
        "version_number": 3,
        "state": "uploading",
        "size_bytes": 482_113,
        "sha256": SHA,
        "content_type": ContentType.PDF.value,
        "scan_status": "not_required",
        "failure_code": None,
        "uploaded_by": OID,
        "uploaded_by_name": "Carol",
        "uploaded_via_project_id": PROJECT,
        "project_name": "Travel",
        "reserved_at": NOW,
        "committed_at": None,
        "ingested_at": None,
        "document_pdf_status": None,
    }
    return SimpleNamespace(**(values | changes))


def upload_version(**changes: Any) -> UploadVersion:
    return replace(uploads._upload_version(version_row()), **changes)


# --- Statements -------------------------------------------------------------------------------


def test_the_gate_reads_the_status_and_seeks_a_clean_canary() -> None:
    assert sql(uploads.INDEX_STATUS) == (
        "SELECT app.search_indexes.status FROM app.search_indexes WHERE app.search_indexes.id = ?"
    )
    canary = sql(uploads.CLEAN_CANARY)
    assert canary.startswith("SELECT TOP ")
    assert canary.endswith(
        "1 FROM app.scan_canaries WHERE app.scan_canaries.result = N'clean' AND "
        "app.scan_canaries.written_at > dateadd(hour, -48, SYSUTCDATETIME())"
    )


def test_the_preflight_query_is_one_union_of_two_seeks() -> None:
    text = sql(uploads.PREFLIGHT_QUERY)
    name_branch, hash_branch = text.split(" UNION ALL ")
    assert name_branch.startswith(
        "SELECT N'name' AS match_kind, d.id AS document_id, d.normalized_name, d.source_name, "
        "v.sha256, v.version_number, "
        "CAST(CASE WHEN (v.id = d.current_version_id) THEN 1 ELSE 0 END AS BIT) AS is_current "
        "FROM app.documents AS d LEFT OUTER JOIN app.document_versions AS v "
        "ON v.document_id = d.id AND v.state IN (N'pending', N'ingesting', N'ingested') "
        "WHERE d.search_index_id = ? AND d.is_deleted = 0 AND d.normalized_name IN ("
    )
    assert hash_branch.startswith("SELECT N'hash', v.document_id, d.normalized_name")
    assert (
        "FROM app.document_versions AS v JOIN app.documents AS d ON d.id = v.document_id "
        "WHERE v.search_index_id = ? AND v.sha256 IN ("
    ) in hash_branch
    assert hash_branch.endswith(
        "AND v.state IN (N'pending', N'ingesting', N'ingested') AND d.is_deleted = 0"
    )


def test_the_project_check_reads_an_active_mapping() -> None:
    assert sql(uploads.PROJECT_MAPPED_QUERY) == (
        "SELECT 1 FROM app.project_indexes AS pi JOIN app.projects AS p ON p.id = pi.project_id "
        "WHERE pi.project_id = ? AND pi.search_index_id = ? AND p.status = N'active'"
    )


def test_the_reservation_statements_lock_and_take_the_number_under_the_lock() -> None:
    assert sql(uploads.DOCUMENT_BY_NAME) == (
        "SELECT app.documents.id, app.documents.is_deleted, app.documents.source_name "
        "FROM app.documents WITH (UPDLOCK, ROWLOCK) WHERE app.documents.search_index_id = ? "
        "AND app.documents.normalized_name = ?"
    )
    assert sql(uploads.RESTORE_DOCUMENT) == (
        "UPDATE app.documents SET source_name=?, is_deleted=0, deleted_at=NULL, deleted_by=NULL, "
        "updated_at=SYSUTCDATETIME(), updated_by=? WHERE app.documents.id = ?"
    )
    assert "WITH (UPDLOCK, ROWLOCK)" in sql(uploads.OPEN_UPLOAD)
    assert "state = N'uploading'" in sql(uploads.OPEN_UPLOAD)
    assert sql(uploads.NEXT_VERSION_NUMBER) == (
        "UPDATE app.documents SET next_version_number=(app.documents.next_version_number + 1) "
        "OUTPUT deleted.next_version_number WHERE app.documents.id = ?"
    )
    insert_version = sql(uploads.INSERT_VERSION)
    assert "OUTPUT" not in insert_version
    assert insert_version.endswith("N'uploading', ?, ?)")


def test_the_commit_and_the_failure_mark_are_guarded_by_the_state() -> None:
    assert sql(uploads.COMMIT_VERSION) == (
        "UPDATE app.document_versions SET blob_last_modified=?, state=N'pending', "
        "committed_at=SYSUTCDATETIME() WHERE app.document_versions.id = ? "
        "AND app.document_versions.state = N'uploading'"
    )
    assert sql(uploads.FAIL_VERSION) == (
        "UPDATE app.document_versions SET state=N'failed', failure_code=?, "
        "failed_at=SYSUTCDATETIME() WHERE app.document_versions.id = ? "
        "AND app.document_versions.state = N'uploading'"
    )
    for statement in (uploads.COMMIT_VERSION, uploads.FAIL_VERSION, uploads.REFRESH_UPLOAD):
        assert "scan_" not in sql(statement)


def test_every_function_is_registered_for_the_plan_audit() -> None:
    functions = [
        "read_upload_gate",
        "read_preflight_matches",
        "check_project_mapped",
        "reserve_upload",
        "read_upload_version",
        "commit_upload",
        "fail_upload",
        "submit_uploads",
    ]
    for name in functions:
        entry = REQUEST_PATH_REGISTRY[f"docchat_core.sql.repositories.uploads.{name}"]
        parameters = inspect.signature(entry.function).parameters
        assert set(entry.example_params) == set(parameters) - {"engine"}


def test_sql_time_is_naive_utc() -> None:
    aware = datetime(2026, 10, 31, 10, 0, tzinfo=timezone(timedelta(hours=1)))
    assert uploads.sql_time(aware) == datetime(2026, 10, 31, 9, 0)  # noqa: DTZ001
    with pytest.raises(ValueError, match="naive"):
        uploads.sql_time(datetime(2026, 10, 31))  # noqa: DTZ001


class ScriptedConnectionAnswer:
    """A unit-of-work result for the engine fake that runs the work on a scripted connection."""

    def __init__(self, *results: Result) -> None:
        self.connection = ScriptedConnection(*results)


@pytest.fixture(autouse=True)
def _run_scripted_work(fake_sql: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Teach the engine fake to run a unit of work on a queued scripted connection."""
    original = fake_sql.run

    async def run(work: Any, *, name: str, retry: str) -> Any:
        result = await original(lambda value: value, name=name, retry=retry)
        fake_sql.calls[-1].statement = work
        if isinstance(result, ScriptedConnectionAnswer):
            return work(connection_of(result.connection))
        return result

    monkeypatch.setattr(fake_sql, "run", run)


# --- The gate ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "canary", "expected"),
    [
        (None, [1], None),
        ("deleted", [1], None),
        ("ready", [1], UploadGate(IndexStatus.READY, scanner_ready=True)),
        ("failed", [], UploadGate(IndexStatus.FAILED, scanner_ready=False)),
    ],
)
def test_the_gate_with_scanning_on(
    status: str | None, canary: list[int], expected: UploadGate | None
) -> None:
    fake = ScriptedConnection(rows(*([status] if status else [])), rows(*canary))
    gate = uploads._gate(connection_of(fake), index_id=INDEX, scanning_enabled=True)
    fake.finished()
    assert gate == expected
    assert fake.executed[0].parameters == {"index_id": INDEX}


def test_the_gate_with_scanning_off_reads_no_canary() -> None:
    fake = ScriptedConnection(rows("provisioning"))
    gate = uploads._gate(connection_of(fake), index_id=INDEX, scanning_enabled=False)
    fake.finished()
    assert gate == UploadGate(IndexStatus.PROVISIONING, scanner_ready=True)
    assert fake.statements() == [uploads.INDEX_STATUS]


async def test_the_gate_is_one_retried_unit(fake_sql: Any) -> None:
    fake_sql.queue("upload_gate", ScriptedConnectionAnswer(rows("ready"), rows()))
    gate = await uploads.read_upload_gate(fake_sql, index_id=INDEX, scanning_enabled=True)
    assert gate == UploadGate(IndexStatus.READY, scanner_ready=False)
    assert (fake_sql.calls[0].name, fake_sql.calls[0].retry) == ("upload_gate", "transient")


# --- Preflight --------------------------------------------------------------------------------


async def test_the_preflight_rows_become_matches(fake_sql: Any) -> None:
    fake_sql.queue(
        "preflight_matches",
        [
            SimpleNamespace(
                match_kind="name",
                document_id=DOCUMENT,
                normalized_name="travel policy.pdf",
                source_name="Travel Policy.pdf",
                sha256=bytearray(SHA),
                version_number=2,
                is_current=1,
            ),
            SimpleNamespace(
                match_kind="name",
                document_id=DOCUMENT,
                normalized_name="travel policy.pdf",
                source_name="Travel Policy.pdf",
                sha256=None,
                version_number=None,
                is_current=0,
            ),
        ],
    )
    matches = await uploads.read_preflight_matches(
        fake_sql, index_id=INDEX, names=["travel policy.pdf"], hashes=[SHA]
    )
    assert matches == [
        PreflightMatch("name", DOCUMENT, "travel policy.pdf", "Travel Policy.pdf", SHA, 2, True),
        PreflightMatch(
            "name", DOCUMENT, "travel policy.pdf", "Travel Policy.pdf", None, None, False
        ),
    ]
    call = fake_sql.calls[0]
    assert call.parameters == {"index_id": INDEX, "names": ["travel policy.pdf"], "hashes": [SHA]}
    assert call.statement is uploads.PREFLIGHT_QUERY


@pytest.mark.parametrize(
    ("names", "hashes"),
    [([], [SHA]), (["a.pdf"], []), (["a.pdf"] * 101, [SHA]), (["a.pdf"], [SHA] * 101)],
)
async def test_the_preflight_lists_hold_1_to_100_values(
    fake_sql: Any, names: list[str], hashes: list[bytes]
) -> None:
    with pytest.raises(ValueError, match="1 to 100"):
        await uploads.read_preflight_matches(fake_sql, index_id=INDEX, names=names, hashes=hashes)
    assert fake_sql.calls == []


@pytest.mark.parametrize(("row", "mapped"), [(SimpleNamespace(), True), (None, False)])
async def test_the_project_check(fake_sql: Any, row: object, mapped: bool) -> None:
    fake_sql.queue("project_mapped", row)
    assert (
        await uploads.check_project_mapped(fake_sql, project_id=PROJECT, index_id=INDEX) is mapped
    )
    assert fake_sql.calls[0].parameters == {"project_id": PROJECT, "index_id": INDEX}


# --- Reserve ----------------------------------------------------------------------------------


def reserve(fake: ScriptedConnection, **changes: Any) -> Reservation:
    values: dict[str, Any] = {
        "index_id": INDEX,
        "name": NAME,
        "size_bytes": 482_113,
        "sha256": SHA,
        "project_id": PROJECT,
        "oid": OID,
        "confirm_update": False,
        "scan_required": False,
    }
    return uploads._reserve(connection_of(fake), **(values | changes))


@pytest.mark.parametrize(
    ("scan_required", "scan_status"), [(True, "awaiting"), (False, "not_required")]
)
def test_a_first_reservation_creates_the_document_and_version_1(
    scan_required: bool, scan_status: str
) -> None:
    fake = ScriptedConnection(rows(), rows(), rows(), rows(), rows(1))
    reservation = reserve(fake, scan_required=scan_required)
    fake.finished()
    assert reservation.version_number == 1
    assert reservation.reused is False
    assert fake.statements() == [
        uploads.DOCUMENT_BY_NAME,
        uploads.INSERT_DOCUMENT,
        uploads.ACTIVE_VERSIONS,
        uploads.DUPLICATE_ELSEWHERE,
        uploads.OPEN_UPLOAD,
        uploads.NEXT_VERSION_NUMBER,
        uploads.INSERT_VERSION,
    ]
    assert fake.executed[1].parameters == {
        "index_id": INDEX,
        "normalized_name": "travel policy.pdf",
        "document_id": reservation.document_id,
        "source_name": "Travel Policy.pdf",
        "oid": OID,
    }
    assert reservation.document_id.version == 4
    assert reservation.version_id.version == 4
    assert fake.executed[4].parameters == {
        "document_id": reservation.document_id,
        "sha256": SHA,
        "scan_required": scan_required,
    }
    assert fake.executed[6].parameters == {
        "version_id": reservation.version_id,
        "document_id": reservation.document_id,
        "index_id": INDEX,
        "version_number": 1,
        "sha256": SHA,
        "size_bytes": 482_113,
        "content_type": "application/pdf",
        "source_name": "Travel Policy.pdf",
        "blob_path": f"{INDEX}/{reservation.document_id}/1/source",
        "oid": OID,
        "project_id": PROJECT,
        "scan_required": scan_required,
        "scan_status": scan_status,
    }


def test_a_deleted_name_comes_back_under_its_document() -> None:
    found = SimpleNamespace(id=DOCUMENT, is_deleted=True, source_name="travel policy.pdf")
    fake = ScriptedConnection(rows(found), count(1), rows(), rows(), rows(), rows(4))
    reservation = reserve(fake)
    fake.finished()
    assert (reservation.document_id, reservation.version_number) == (DOCUMENT, 4)
    assert fake.statements()[1] is uploads.RESTORE_DOCUMENT
    assert fake.executed[1].parameters == {
        "document_id": DOCUMENT,
        "source_name": "Travel Policy.pdf",
        "oid": OID,
    }


@pytest.mark.parametrize(
    ("stored", "renamed"), [("Travel Policy.pdf", False), ("travel  POLICY.pdf", True)]
)
def test_a_live_document_takes_the_reservations_display_name(stored: str, renamed: bool) -> None:
    found = SimpleNamespace(id=DOCUMENT, is_deleted=False, source_name=stored)
    answers = [rows(found), *([count(1)] if renamed else []), rows(), rows(), rows(), rows(2)]
    fake = ScriptedConnection(*answers)
    reserve(fake)
    fake.finished()
    assert (uploads.RENAME_DOCUMENT in fake.statements()) is renamed


@pytest.mark.parametrize(
    ("active", "confirm", "code"),
    [
        ([SHA], True, "content_unchanged"),
        ([OTHER_SHA, SHA], False, "content_unchanged"),
        ([OTHER_SHA], False, "update_not_confirmed"),
    ],
)
def test_the_documents_versions_refuse(active: list[bytes], confirm: bool, code: str) -> None:
    found = SimpleNamespace(id=DOCUMENT, is_deleted=False, source_name="Travel Policy.pdf")
    versions = [SimpleNamespace(id=uuid4(), sha256=sha) for sha in active]
    fake = ScriptedConnection(rows(found), rows(*versions))
    with pytest.raises(UploadRefusedError) as refused:
        reserve(fake, confirm_update=confirm)
    fake.finished()
    assert refused.value.code == code
    assert str(refused.value) == code


def test_a_confirmed_update_reserves_the_next_number() -> None:
    found = SimpleNamespace(id=DOCUMENT, is_deleted=False, source_name="Travel Policy.pdf")
    older = SimpleNamespace(id=uuid4(), sha256=OTHER_SHA)
    fake = ScriptedConnection(rows(found), rows(older), rows(), rows(), rows(7))
    reservation = reserve(fake, confirm_update=True)
    fake.finished()
    assert (reservation.version_number, reservation.reused) == (7, False)


def test_the_same_bytes_under_another_name_are_a_duplicate() -> None:
    fake = ScriptedConnection(rows(), rows(), rows(SimpleNamespace(document_id=uuid4())))
    with pytest.raises(UploadRefusedError) as refused:
        reserve(fake)
    fake.finished()
    assert refused.value.code == "duplicate_content"
    duplicate = fake.executed[-1]
    assert duplicate.statement is uploads.DUPLICATE_ELSEWHERE
    assert duplicate.parameters["sha256"] == SHA


def test_an_uploading_version_of_the_same_bytes_is_reused() -> None:
    found = SimpleNamespace(id=DOCUMENT, is_deleted=False, source_name="Travel Policy.pdf")
    open_version = SimpleNamespace(id=VERSION, version_number=5)
    fake = ScriptedConnection(rows(found), rows(), rows(), rows(open_version), count(1))
    reservation = reserve(fake, scan_required=True)
    fake.finished()
    assert reservation == Reservation(DOCUMENT, VERSION, 5, reused=True)
    assert fake.statements()[-1] is uploads.REFRESH_UPLOAD
    assert fake.executed[-1].parameters == {
        "version_id": VERSION,
        "oid": OID,
        "project_id": PROJECT,
    }


def _name_race() -> SqlUniqueViolation:
    return SqlUniqueViolation(2627, "23000", "UQ_documents_index_name")


async def test_a_lost_name_race_reruns_once(fake_sql: Any) -> None:
    reservation = Reservation(DOCUMENT, VERSION, 1, reused=False)
    fake_sql.queue("reserve_upload", _name_race(), reservation)
    result = await uploads.reserve_upload(
        fake_sql,
        index_id=INDEX,
        name=NAME,
        size_bytes=1,
        sha256=SHA,
        project_id=PROJECT,
        oid=OID,
        confirm_update=False,
        scan_required=False,
    )
    assert result == reservation
    assert [(call.name, call.retry) for call in fake_sql.calls] == [
        ("reserve_upload", "transient"),
        ("reserve_upload", "transient"),
    ]


@pytest.mark.parametrize(
    "failures",
    [
        [_name_race(), _name_race()],
        [SqlUniqueViolation(2627, "23000", "UQ_document_versions_number")],
    ],
)
async def test_a_second_race_or_another_key_raises(
    fake_sql: Any, failures: list[SqlUniqueViolation]
) -> None:
    fake_sql.queue("reserve_upload", *failures)
    with pytest.raises(SqlUniqueViolation):
        await uploads.reserve_upload(
            fake_sql,
            index_id=INDEX,
            name=NAME,
            size_bytes=1,
            sha256=SHA,
            project_id=PROJECT,
            oid=OID,
            confirm_update=False,
            scan_required=False,
        )
    assert len(fake_sql.calls) == len(failures)


# --- Content ----------------------------------------------------------------------------------


async def test_the_version_of_the_index_with_its_names(fake_sql: Any) -> None:
    fake_sql.queue("upload_version", version_row(document_pdf_status="built", ingested_at=NOW))
    version = await uploads.read_upload_version(fake_sql, index_id=INDEX, version_id=VERSION)
    assert version == UploadVersion(
        id=VERSION,
        document_id=DOCUMENT,
        index_id=INDEX,
        version_number=3,
        state=DocumentVersionState.UPLOADING,
        size_bytes=482_113,
        sha256=SHA,
        content_type=ContentType.PDF,
        scan_status=ScanStatus.NOT_REQUIRED,
        failure_code=None,
        uploaded_by=OID,
        uploaded_by_name="Carol",
        project_id=PROJECT,
        project_name="Travel",
        reserved_at=NOW.replace(tzinfo=UTC),
        committed_at=None,
        ingested_at=NOW.replace(tzinfo=UTC),
        document_pdf_status=DocumentPdfStatus.BUILT,
    )
    assert fake_sql.calls[0].parameters == {"index_id": INDEX, "version_id": VERSION}


async def test_a_version_of_another_index_is_none(fake_sql: Any) -> None:
    fake_sql.queue("upload_version", None)
    assert await uploads.read_upload_version(fake_sql, index_id=INDEX, version_id=VERSION) is None


def test_the_commit_writes_its_audit_row_in_the_transaction() -> None:
    committed = version_row(state="pending", committed_at=NOW)
    fake = ScriptedConnection(count(1), rows(committed))
    last_modified = datetime(2026, 10, 31, 9, 15, 2, tzinfo=UTC)
    provider = TracerProvider()
    with provider.get_tracer("test").start_as_current_span("request") as span:
        trace_id = format(span.get_span_context().trace_id, "032x")
        result = uploads._commit(
            connection_of(fake), version=upload_version(), last_modified=last_modified, actor=OID
        )
    fake.finished()
    assert result.outcome == "committed"
    assert result.version is not None
    assert result.version.state is DocumentVersionState.PENDING
    assert fake.executed[0].parameters == {
        "version_id": VERSION,
        "last_modified": datetime(2026, 10, 31, 9, 15, 2),  # noqa: DTZ001
    }
    audit = fake.executed[1]
    assert isinstance(audit.statement, Insert)
    assert audit.statement.table is audit_events
    assert audit.parameters == {
        "actor_type": "user",
        "actor_object_id": OID,
        "subject_object_id": None,
        "action": "document.upload_committed",
        "target_type": "document_version",
        "target_id": str(VERSION),
        "project_id": PROJECT,
        "metadata_json": json.dumps(
            {
                "indexId": str(INDEX),
                "documentId": str(DOCUMENT),
                "versionNumber": 3,
                "sizeBytes": 482_113,
                "contentType": "application/pdf",
            },
            separators=(",", ":"),
        ),
        "correlation_id": trace_id,
    }
    assert [event.action for event in result.events] == ["document.upload_committed"]


@pytest.mark.parametrize(
    ("after", "outcome"),
    [
        (version_row(state="pending", committed_at=NOW), "already_pending"),
        (version_row(state="failed", failure_code="document_deleted"), "not_uploading"),
        (None, "not_uploading"),
    ],
)
def test_a_commit_that_changes_nothing_writes_nothing(
    after: SimpleNamespace | None, outcome: str
) -> None:
    fake = ScriptedConnection(count(0), rows(*([after] if after else [])))
    result = uploads._commit(
        connection_of(fake),
        version=upload_version(),
        last_modified=NOW.replace(tzinfo=UTC),
        actor=OID,
    )
    fake.finished()
    assert result.outcome == outcome
    assert result.events == ()
    assert not any(isinstance(entry.statement, Insert) for entry in fake.executed)


@pytest.mark.parametrize(
    ("constraint", "code"),
    [
        ("UX_document_versions_document_sha", "content_unchanged"),
        ("UX_document_versions_index_sha", "duplicate_content"),
    ],
)
async def test_a_sha256_index_refusal_names_its_code(
    fake_sql: Any, constraint: str, code: str
) -> None:
    fake_sql.queue("commit_upload", SqlUniqueViolation(2601, "23000", constraint))
    result = await uploads.commit_upload(
        fake_sql, version=upload_version(), last_modified=NOW.replace(tzinfo=UTC), actor=OID
    )
    assert result == CommitResult("duplicate", code=code)  # type: ignore[arg-type]
    assert fake_sql.calls[0].retry == "transient"


async def test_another_key_violation_at_commit_raises(fake_sql: Any) -> None:
    fake_sql.queue("commit_upload", SqlUniqueViolation(2627, "23000", None))
    with pytest.raises(SqlUniqueViolation):
        await uploads.commit_upload(
            fake_sql, version=upload_version(), last_modified=NOW.replace(tzinfo=UTC), actor=OID
        )


@pytest.mark.parametrize(("changed", "marked"), [(1, True), (0, False)])
async def test_the_failure_mark(fake_sql: Any, changed: int, marked: bool) -> None:
    executed: list[tuple[str, Any, str]] = []

    async def execute(statement: Any, parameters: Any, *, name: str, retry: str) -> int:
        executed.append((name, parameters, retry))
        assert statement is uploads.FAIL_VERSION
        return changed

    fake_sql.execute = execute
    assert (
        await uploads.fail_upload(fake_sql, version_id=VERSION, code="content_unchanged") is marked
    )
    assert executed == [
        ("fail_upload", {"version_id": VERSION, "code": "content_unchanged"}, "transient")
    ]


# --- Submit -----------------------------------------------------------------------------------


def test_a_submitted_run_writes_its_audit_row() -> None:
    queued, absent, active = uuid4(), uuid4(), uuid4()
    fake = ScriptedConnection(
        rows(SimpleNamespace(id=queued, document_id=DOCUMENT, state="pending")),
        rows(),
        rows(active),
    )
    submitted = uploads._submit(
        connection_of(fake), index_id=INDEX, version_ids=[queued, absent], actor=OID, trace=TRACE
    )
    fake.finished()
    run_id = submitted.result.run_id
    assert run_id is not None
    assert [result.outcome for result in submitted.result.results] == [
        SubmitOutcome.QUEUED,
        SubmitOutcome.NOT_FOUND,
    ]
    (audit,) = [entry for entry in fake.executed if entry.sql.startswith("INSERT INTO app.audit")]
    assert audit.parameters["action"] == "ingestion.run_submitted"
    assert audit.parameters["target_type"] == "ingestion_run"
    assert audit.parameters["target_id"] == str(run_id)
    assert audit.parameters["project_id"] is None
    assert json.loads(audit.parameters["metadata_json"]) == {
        "indexId": str(INDEX),
        "itemCount": 1,
        "notQueuedCount": 1,
    }
    assert [event.action for event in submitted.events] == ["ingestion.run_submitted"]


def test_nothing_to_queue_writes_no_audit_row() -> None:
    fake = ScriptedConnection(rows(), rows())
    submitted = uploads._submit(
        connection_of(fake), index_id=INDEX, version_ids=[uuid4()], actor=OID, trace=None
    )
    fake.finished()
    assert submitted.result.run_id is None
    assert submitted.events == ()
    assert not any(isinstance(entry.statement, Insert) for entry in fake.executed)


def test_only_a_created_run_is_audited() -> None:
    with pytest.raises(ValueError, match="created run"):
        uploads.run_submitted_event(INDEX, UploadRunResult(run_id=None, results=()), OID)


async def test_a_submit_is_never_rerun_as_a_whole(fake_sql: Any) -> None:
    submitted = SubmittedUploads(UploadRunResult(run_id=None, results=()))
    fake_sql.queue("submit_uploads", submitted)
    result = await uploads.submit_uploads(
        fake_sql, index_id=INDEX, version_ids=[VERSION], actor=OID, trace=TRACE
    )
    assert result is submitted
    assert fake_sql.calls[0].retry == "none"
