"""Unit tests of the four upload operations (T-129's API half, T-132, T-133, T-134, T-136, T-137
and the in-process halves of T-063 and T-160; `14-ingestion.md` section 2; `11-api.md` sections
3.11.5 to 3.11.9).

The app runs in process over the unit suite's fake data-access layer, taught here to answer the
upload statements by statement-set name, and over a fake `documents` container and queue that
record every call; the access-tier test gives a real `ContainerClient` a recording replay
transport instead.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import tracemalloc
from collections import deque
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from os import SEEK_END
from types import SimpleNamespace
from typing import IO, Any, Final, Protocol, cast, override
from uuid import UUID, uuid4

import pytest
from azure.core import MatchConditions
from azure.core.exceptions import (
    HttpResponseError,
    ResourceExistsError,
    ResourceModifiedError,
    ResourceNotFoundError,
    ServiceRequestError,
)
from azure.core.rest import HttpRequest
from azure.storage.blob import BlobBlock, ContentSettings, StandardBlobTier
from azure.storage.blob.aio import ContainerClient
from fastapi import FastAPI
from opentelemetry.sdk.metrics.export import HistogramDataPoint, InMemoryMetricReader
from opentelemetry.sdk.trace import ReadableSpan
from starlette.requests import ClientDisconnect

from docchat_api.app import Ports
from docchat_api.http.queue import send_run_message
from docchat_api.routes import admin_uploads
from docchat_api.routes.admin_common import failure_message
from docchat_api.routes.admin_uploads import (
    BLOCK_SIZE,
    SLOTS_STATE,
    CheckedFile,
    UploadSlots,
    UploadStorage,
    block_id,
    blocks_of,
    check_batch,
    commit_blob,
    declared_length,
    discard_blob,
    duplicate_issues,
    mark_duplicates,
    signature_matches,
    upload_storage,
    wire_block_id,
)
from docchat_api.settings import ProcessSettings
from docchat_core.clients import ClientRegistry
from docchat_core.errors import DocChatProblem
from docchat_core.ingestion.models import (
    ContentType,
    DocumentVersionState,
    IndexStatus,
    IngestionRefusedError,
    PreflightRejection,
    SubmitOutcome,
    TraceContext,
)
from docchat_core.ingestion.runs import SubmitResult, UploadRunResult
from docchat_core.sql.engine import SqlDeadlock, SqlUnavailable, SqlUniqueViolation
from docchat_core.sql.repositories.uploads import (
    CommitResult,
    Reservation,
    SubmittedUploads,
    UploadGate,
    UploadRefusedError,
    UploadVersion,
    _upload_version,
    run_submitted_event,
    upload_committed_event,
)
from docchat_testing.fakes.replay import Exchange, ReplayResponse, ReplayTransport
from docchat_testing.fixtures.api import ApiUnderTest, api_settings, running_api
from docchat_testing.fixtures.identities import SharedPrincipal
from docchat_testing.tokens import SigningKeys

INDEX: Final = UUID("4d8f0a71-2b3c-4d5e-8f60-718293a4b5c6")
PROJECT: Final = UUID("5e9a1b82-3c4d-4e6f-9a71-8293a4b5c6d7")
DOCUMENT: Final = UUID("6fab2c93-4d5e-4f70-8b82-93a4b5c6d7e8")
VERSION: Final = UUID("7abc3da4-5e6f-4081-9c93-a4b5c6d7e8f9")
UPLOADER: Final = UUID("0a11ce00-0000-4000-8000-000000000003")
READ_AT: Final = datetime(2026, 10, 31, 9, 0, 0, 0)  # noqa: DTZ001 - a DATETIME2 value as read
LAST_MODIFIED: Final = datetime(2026, 10, 31, 9, 15, 2, tzinfo=UTC)
EARLIER: Final = datetime(2026, 10, 31, 9, 14, 0, tzinfo=UTC)
OCTET: Final = {"Content-Type": "application/octet-stream"}
MIB: Final = 1024 * 1024
MAX_BYTES: Final = 52_428_800
BASE: Final = f"/v1/admin/indexes/{INDEX}/uploads"
CONTENT_URL: Final = f"{BASE}/{VERSION}/content"


class Captured(Protocol):
    def finished_spans(self, name: str | None = None) -> list[ReadableSpan]: ...

    def server_spans(self) -> list[ReadableSpan]: ...

    def points(self, metric_name: str) -> dict[tuple[tuple[str, object], ...], float]: ...

    def events(self, name: str) -> list[dict[str, Any]]: ...

    @property
    def metrics(self) -> InMemoryMetricReader: ...


def histogram_points(
    captured: Captured, metric_name: str
) -> dict[tuple[tuple[str, object], ...], tuple[int, float]]:
    """A histogram's points as (count, sum) by attributes."""
    data = captured.metrics.get_metrics_data()
    found: dict[tuple[tuple[str, object], ...], tuple[int, float]] = {}
    for resource in [] if data is None else data.resource_metrics:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                if metric.name == metric_name:
                    for point in metric.data.data_points:
                        assert isinstance(point, HistogramDataPoint)
                        key = tuple(sorted((point.attributes or {}).items()))
                        found[key] = (point.count, point.sum)
    return found


def sha(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def pdf_bytes(size: int) -> bytes:
    return (b"%PDF-1.7\n" + b"0" * size)[:size]


# --- The fake upload statements ---------------------------------------------------------------


def version_row(data: bytes = b"", **changes: Any) -> SimpleNamespace:
    """A version row as the driver returns it; `data` sets the reserved size and SHA-256."""
    content = data or pdf_bytes(1000)
    values: dict[str, Any] = {
        "id": VERSION,
        "document_id": DOCUMENT,
        "search_index_id": INDEX,
        "version_number": 3,
        "state": "uploading",
        "size_bytes": len(content),
        "sha256": sha(content),
        "content_type": ContentType.PDF.value,
        "scan_status": "not_required",
        "failure_code": None,
        "uploaded_by": UPLOADER,
        "uploaded_by_name": "Carol",
        "uploaded_via_project_id": PROJECT,
        "project_name": "Travel",
        "reserved_at": READ_AT,
        "committed_at": None,
        "ingested_at": None,
        "document_pdf_status": None,
    }
    return SimpleNamespace(**(values | changes))


def pending(version: UploadVersion) -> UploadVersion:
    return replace(version, state=DocumentVersionState.PENDING, committed_at=LAST_MODIFIED)


@dataclass
class UploadSql:
    """The answers of the upload statements; every call is recorded by name with its bound
    parameters or its unit of work's keywords."""

    gate: UploadGate | None = field(
        default_factory=lambda: UploadGate(IndexStatus.READY, scanner_ready=True)
    )
    preflight: list[SimpleNamespace] = field(default_factory=list)
    mapped: bool = True
    reservations: deque[object] = field(default_factory=deque)
    version: SimpleNamespace | None = field(default_factory=version_row)
    commits: deque[object] = field(default_factory=deque)
    fail_marks: deque[object] = field(default_factory=deque)
    submitted: deque[object] = field(default_factory=deque)
    calls: list[tuple[str, Any]] = field(default_factory=list)

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def call(self, name: str) -> Any:
        found = [values for called, values in self.calls if called == name]
        assert len(found) == 1, (name, self.names())
        return found[0]

    @staticmethod
    def _answer(queued: deque[object], default: Callable[[], object] | None = None) -> Any:
        answer = queued.popleft() if queued or default is None else default()
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def default_commit(self) -> CommitResult:
        assert self.version is not None
        version = _upload_version(self.version)
        return CommitResult(
            "committed",
            pending(version),
            events=(upload_committed_event(version, UPLOADER),),
        )


@pytest.fixture
def upload_sql(fake_sql: Any, monkeypatch: pytest.MonkeyPatch) -> UploadSql:
    """The unit suite's fake data-access layer, answering the upload statements too."""
    script = UploadSql()
    run, fetch_one, fetch_all = fake_sql.run, fake_sql.fetch_one, fake_sql.fetch_all

    async def run_upload(work: Any, *, name: str, retry: str) -> Any:
        if name == "upload_gate":
            script.calls.append((name, work.keywords))
            return script.gate
        queues: dict[str, tuple[deque[object], Callable[[], object] | None]] = {
            "reserve_upload": (script.reservations, None),
            "commit_upload": (script.commits, script.default_commit),
            "submit_uploads": (script.submitted, None),
        }
        if name in queues:
            script.calls.append((name, work.keywords))
            queued, default = queues[name]
            return script._answer(queued, default)
        return await run(work, name=name, retry=retry)

    async def fetch_one_upload(
        statement: object, parameters: Any = None, *, name: str, retry: str = "transient"
    ) -> Any:
        if name == "project_mapped":
            script.calls.append((name, parameters))
            return SimpleNamespace() if script.mapped else None
        if name == "upload_version":
            script.calls.append((name, parameters))
            return script.version
        return await fetch_one(statement, parameters, name=name, retry=retry)

    async def fetch_all_upload(
        statement: object, parameters: Any = None, *, name: str, retry: str = "transient"
    ) -> Any:
        if name == "preflight_matches":
            script.calls.append((name, parameters))
            return script.preflight
        return await fetch_all(statement, parameters, name=name, retry=retry)

    async def execute_upload(statement: object, parameters: Any, *, name: str, retry: str) -> int:
        assert name == "fail_upload"
        script.calls.append((name, parameters))
        return int(script._answer(script.fail_marks, lambda: 1))

    monkeypatch.setattr(fake_sql, "run", run_upload)
    monkeypatch.setattr(fake_sql, "fetch_one", fetch_one_upload)
    monkeypatch.setattr(fake_sql, "fetch_all", fetch_all_upload)
    monkeypatch.setattr(fake_sql, "execute", execute_upload, raising=False)
    return script


# --- The fake storage -------------------------------------------------------------------------


@dataclass
class FakeBlob:
    path: str
    container: FakeContainer

    async def stage_block(self, block_id: str, data: IO[bytes]) -> object:
        # The end offset is the block's length; reading it would copy the block.
        self.container.staged.append((self.path, block_id, data.seek(0, SEEK_END)))
        if self.container.stage_error is not None:
            raise self.container.stage_error
        return {}

    async def commit_block_list(self, block_list: list[BlobBlock], **kwargs: Any) -> Any:
        self.container.commits.append((self.path, [block.id for block in block_list], kwargs))
        if self.container.commit_error is not None:
            raise self.container.commit_error
        return {"last_modified": LAST_MODIFIED, "etag": '"0x8D"'}

    async def get_blob_properties(self) -> Any:
        self.container.property_reads.append(self.path)
        if self.container.properties_error is not None:
            raise self.container.properties_error
        return SimpleNamespace(last_modified=EARLIER)

    async def delete_blob(self) -> None:
        self.container.deleted.append(self.path)
        if self.container.delete_error is not None:
            raise self.container.delete_error


@dataclass
class FakeContainer:
    staged: list[tuple[str, str, int]] = field(default_factory=list)
    commits: list[tuple[str, list[str], dict[str, Any]]] = field(default_factory=list)
    property_reads: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    stage_error: BaseException | None = None
    commit_error: BaseException | None = None
    properties_error: BaseException | None = None
    delete_error: BaseException | None = None

    def get_blob_client(self, blob: str) -> FakeBlob:
        return FakeBlob(blob, self)


@dataclass
class FakeQueue:
    sent: list[tuple[str, int]] = field(default_factory=list)
    error: BaseException | None = None

    async def send_message(self, content: str, *, time_to_live: int) -> object:
        self.sent.append((content, time_to_live))
        if self.error is not None:
            raise self.error
        return {}


@pytest.fixture
def container() -> FakeContainer:
    return FakeContainer()


@pytest.fixture
def queue() -> FakeQueue:
    return FakeQueue()


def condition_not_met(code: str = "ConditionNotMet") -> ResourceModifiedError:
    error = ResourceModifiedError(message="precondition")
    error.error_code = code  # type: ignore[attr-defined]
    return error


# --- The app ----------------------------------------------------------------------------------


def settings_with(**upload: Any) -> ProcessSettings:
    settings = api_settings()
    return replace(settings, upload=settings.upload.model_copy(update=upload))


@pytest.fixture
def settings() -> ProcessSettings:
    return api_settings()


@asynccontextmanager
async def start(
    ports: Ports,
    signing_keys: SigningKeys,
    settings: ProcessSettings,
    storage: UploadStorage,
) -> AsyncIterator[ApiUnderTest]:
    async with running_api(settings=settings, ports=ports, signing_keys=signing_keys) as api:
        api.app.dependency_overrides[upload_storage] = lambda: storage
        yield api


@pytest.fixture
async def api(
    *,
    ports: Ports,
    signing_keys: SigningKeys,
    settings: ProcessSettings,
    captured: Captured,
    upload_sql: UploadSql,
    container: FakeContainer,
    queue: FakeQueue,
) -> AsyncIterator[ApiUnderTest]:
    storage = UploadStorage(
        documents=cast("Any", container),
        queue=cast("Any", queue),
    )
    async with start(ports, signing_keys, settings, storage) as started:
        yield started


def slots_of(app: FastAPI) -> UploadSlots:
    slots = getattr(app.state, SLOTS_STATE)
    assert isinstance(slots, UploadSlots)
    return slots


def preflight_file(name: str, data_sha: bytes, size: int = 1000) -> dict[str, Any]:
    return {"name": name, "sizeBytes": size, "sha256": data_sha.hex()}


def reservation_body(**changes: Any) -> dict[str, Any]:
    return {
        "name": "Travel Policy.pdf",
        "sizeBytes": 1000,
        "sha256": sha(pdf_bytes(1000)).hex().upper(),
        "projectId": str(PROJECT),
        "confirmUpdate": False,
    } | changes


async def put_content(
    api: ApiUnderTest,
    carol: SharedPrincipal,
    content: bytes | AsyncIterator[bytes],
    *,
    headers: Mapping[str, str] | None = None,
) -> Any:
    sent = {**OCTET, **carol.authorization(), **(headers or {})}
    return await api.client.put(CONTENT_URL, content=content, headers=sent)


# --- The gate (in-process halves of T-063 and T-160) -----------------------------------------

OPERATIONS: Final = [
    ("POST", f"{BASE}/preflight", {"files": [preflight_file("a.pdf", bytes(32))]}),
    ("POST", BASE, reservation_body()),
    ("PUT", CONTENT_URL, None),
    ("POST", f"{BASE}/submit", {"versionIds": [str(VERSION)]}),
]


def _send(api: ApiUnderTest, carol: SharedPrincipal, method: str, url: str, body: object) -> Any:
    headers = carol.authorization()
    if body is None:
        return api.client.request(method, url, content=pdf_bytes(1000), headers=headers | OCTET)
    return api.client.request(method, url, json=body, headers=headers)


@pytest.mark.parametrize(("method", "url", "body"), OPERATIONS)
async def test_t160_a_missing_or_deleted_index_is_not_found(
    *,
    api: ApiUnderTest,
    carol: SharedPrincipal,
    upload_sql: UploadSql,
    method: str,
    url: str,
    body: object,
) -> None:
    upload_sql.gate = None

    response = await _send(api, carol, method, url, body)

    assert response.status_code == 404
    assert response.json()["code"] == "index_not_found"
    assert upload_sql.names() == ["upload_gate"]


@pytest.mark.parametrize(("method", "url", "body"), OPERATIONS)
async def test_t160_uploads_pause_without_a_clean_canary_and_write_nothing(
    *,
    api: ApiUnderTest,
    carol: SharedPrincipal,
    upload_sql: UploadSql,
    container: FakeContainer,
    method: str,
    url: str,
    body: object,
) -> None:
    upload_sql.gate = UploadGate(IndexStatus.READY, scanner_ready=False)

    response = await _send(api, carol, method, url, body)

    assert response.status_code == 503
    problem = response.json()
    assert (problem["code"], problem["retryAfterSeconds"]) == ("malware_scanning_not_ready", 3600)
    assert response.headers["Retry-After"] == "3600"
    assert upload_sql.names() == ["upload_gate"]
    assert container.staged == []


@pytest.mark.parametrize("scanning", [True, False])
async def test_t160_the_gate_reads_the_scanning_setting(
    *,
    ports: Ports,
    signing_keys: SigningKeys,
    captured: Captured,
    upload_sql: UploadSql,
    carol: SharedPrincipal,
    scanning: bool,
) -> None:
    storage = UploadStorage(documents=cast("Any", FakeContainer()), queue=cast("Any", FakeQueue()))
    settings = settings_with(storage_malware_scanning_enabled=scanning)
    async with start(ports, signing_keys, settings, storage) as api:
        await api.client.post(
            f"{BASE}/preflight",
            json={"files": [preflight_file("a.pdf", bytes(32), size=0)]},
            headers=carol.authorization(),
        )
    assert upload_sql.call("upload_gate") == {"index_id": INDEX, "scanning_enabled": scanning}


@pytest.mark.parametrize("status", [IndexStatus.FAILED, IndexStatus.DELETING])
@pytest.mark.parametrize(("method", "url", "body"), OPERATIONS)
async def test_a_failed_or_deleting_index_takes_no_upload(
    *,
    api: ApiUnderTest,
    carol: SharedPrincipal,
    upload_sql: UploadSql,
    status: IndexStatus,
    method: str,
    url: str,
    body: object,
) -> None:
    upload_sql.gate = UploadGate(status, scanner_ready=True)

    response = await _send(api, carol, method, url, body)

    assert response.status_code == 409
    assert response.json()["code"] == "index_not_accepting_uploads"


@pytest.mark.parametrize(("method", "url", "body"), OPERATIONS)
async def test_a_user_token_is_refused_before_the_body_is_read(
    *,
    api: ApiUnderTest,
    alice: SharedPrincipal,
    upload_sql: UploadSql,
    method: str,
    url: str,
    body: object,
) -> None:
    response = await api.client.request(
        method, url, content=b"{not json", headers=alice.authorization() | OCTET
    )

    assert response.status_code == 403
    assert response.json()["code"] == "admin_required"
    assert upload_sql.calls == []


# --- adminPreflightUploads (T-132, T-137) -----------------------------------------------------

A, B, C, E, F, G = (bytes([value]) * 32 for value in range(1, 7))
D1, D2, D3, D4, D5, D6 = (uuid4() for _ in range(6))


def match(
    kind: str,
    document_id: UUID,
    normalized: str,
    source: str,
    *,
    digest: bytes | None,
    number: int | None,
    current: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        match_kind=kind,
        document_id=document_id,
        normalized_name=normalized,
        source_name=source,
        sha256=digest,
        version_number=number,
        is_current=1 if current else 0,
    )


async def test_t132_preflight_statuses_follow_their_precedence(
    api: ApiUnderTest, carol: SharedPrincipal, upload_sql: UploadSql
) -> None:
    upload_sql.preflight = [
        match("name", D1, "report.pdf", "Report.pdf", digest=A, number=2, current=True),
        match("hash", D2, "second.pdf", "Second.pdf", digest=B, number=1),
        match("name", D3, "plan.pdf", "Plan.pdf", digest=bytes(32), number=1, current=True),
        match("hash", D4, "plan-copy.pdf", "Plan copy.pdf", digest=C, number=4),
        match("name", D5, "notes.docx", "Notes.docx", digest=G, number=1),
        match("name", D6, "new.pptx", "New.pptx", digest=None, number=None),
        match("hash", D1, "report.pdf", "Report.pdf", digest=A, number=2, current=True),
    ]
    files = [
        preflight_file("Report.pdf", A),
        preflight_file("Other.pdf", B),
        preflight_file("Plan.pdf", C),
        preflight_file("Notes.docx", E),
        preflight_file("New.pptx", F),
        preflight_file("bad\u202etxt.pdf", G),
        preflight_file("sheet.xlsx", G),
        preflight_file("empty.pdf", G, size=0),
        preflight_file("big.pdf", G, size=MAX_BYTES + 1),
        preflight_file("  report.PDF ", G),
        preflight_file("copy.pdf", A),
    ]

    response = await api.client.post(
        f"{BASE}/preflight", json={"files": files}, headers=carol.authorization()
    )

    assert response.status_code == 200
    results = response.json()["results"]
    rejected = {
        "status": "rejected",
        "documentId": None,
        "existingDocument": None,
        "currentVersionNumber": None,
    }
    assert results == [
        {
            "name": "Report.pdf",
            "status": "unchanged",
            "reason": None,
            "documentId": str(D1),
            "existingDocument": None,
            "currentVersionNumber": 2,
        },
        {
            "name": "Other.pdf",
            "status": "duplicate",
            "reason": None,
            "documentId": None,
            "existingDocument": {"documentId": str(D2), "name": "Second.pdf"},
            "currentVersionNumber": None,
        },
        {
            "name": "Plan.pdf",
            "status": "duplicate",
            "reason": None,
            "documentId": str(D3),
            "existingDocument": {"documentId": str(D4), "name": "Plan copy.pdf"},
            "currentVersionNumber": 1,
        },
        {
            "name": "Notes.docx",
            "status": "update",
            "reason": None,
            "documentId": str(D5),
            "existingDocument": None,
            "currentVersionNumber": None,
        },
        {
            "name": "New.pptx",
            "status": "new",
            "reason": None,
            "documentId": None,
            "existingDocument": None,
            "currentVersionNumber": None,
        },
        {"name": "bad\u202etxt.pdf", "reason": "invalid_name", **rejected},
        {"name": "sheet.xlsx", "reason": "unsupported_type", **rejected},
        {"name": "empty.pdf", "reason": "empty_file", **rejected},
        {"name": "big.pdf", "reason": "too_large", **rejected},
        {"name": "report.PDF", "reason": "duplicate_in_batch", **rejected},
        {"name": "copy.pdf", "reason": "duplicate_in_batch", **rejected},
    ]
    assert upload_sql.call("preflight_matches") == {
        "index_id": INDEX,
        "names": ["report.pdf", "other.pdf", "plan.pdf", "notes.docx", "new.pptx"],
        "hashes": [A, B, C, E, F],
    }


async def test_t132_a_batch_of_rejected_files_reads_nothing(
    api: ApiUnderTest, carol: SharedPrincipal, upload_sql: UploadSql
) -> None:
    response = await api.client.post(
        f"{BASE}/preflight",
        json={"files": [preflight_file("a.doc", A), preflight_file("b.pdf", B, size=0)]},
        headers=carol.authorization(),
    )

    assert [result["reason"] for result in response.json()["results"]] == [
        "unsupported_type",
        "empty_file",
    ]
    assert upload_sql.names() == ["upload_gate"]


def test_a_rejected_file_doesnt_make_a_later_one_a_duplicate() -> None:
    first = CheckedFile("a.pdf", A, rejection=PreflightRejection.TOO_LARGE)
    second = CheckedFile("A.pdf", A, normalized_name="a.pdf")
    third = CheckedFile("b.pdf", A, normalized_name="b.pdf")
    assert [file.rejection for file in mark_duplicates([first, second, third])] == [
        PreflightRejection.TOO_LARGE,
        None,
        PreflightRejection.DUPLICATE_IN_BATCH,
    ]


async def test_t137_preflight_refuses_more_than_100_files(
    api: ApiUnderTest, carol: SharedPrincipal, upload_sql: UploadSql
) -> None:
    files = [preflight_file(f"{number}.pdf", A) for number in range(101)]

    response = await api.client.post(
        f"{BASE}/preflight", json={"files": files}, headers=carol.authorization()
    )

    assert response.status_code == 422
    assert response.json()["code"] == "validation_failed"
    assert response.json()["errors"] == [{"path": "body.files", "reason": "too_many_items"}]
    assert upload_sql.calls == []


@pytest.mark.parametrize("operation", ["preflight", "submit"])
async def test_t137_the_batch_setting_answers_batch_too_large_after_the_gate(
    *,
    ports: Ports,
    signing_keys: SigningKeys,
    captured: Captured,
    upload_sql: UploadSql,
    carol: SharedPrincipal,
    operation: str,
) -> None:
    storage = UploadStorage(documents=cast("Any", FakeContainer()), queue=cast("Any", FakeQueue()))
    body: dict[str, Any] = (
        {"files": [preflight_file(f"{number}.pdf", A) for number in range(3)]}
        if operation == "preflight"
        else {"versionIds": [str(uuid4()) for _ in range(3)]}
    )
    path = "body.files" if operation == "preflight" else "body.versionIds"
    async with start(
        ports, signing_keys, settings_with(upload_max_files_per_batch=2), storage
    ) as api:
        response = await api.client.post(
            f"{BASE}/{operation}", json=body, headers=carol.authorization()
        )
    assert response.status_code == 422
    problem = response.json()
    assert problem["code"] == "batch_too_large"
    assert problem["detail"] == "Send at most 2 files at a time."
    assert problem["errors"] == [{"path": path, "reason": "too_many_items"}]
    assert upload_sql.names() == ["upload_gate"]


# --- adminReserveUpload -----------------------------------------------------------------------


@pytest.mark.parametrize(("scanning", "reused"), [(True, False), (False, True)])
async def test_a_reservation_answers_201(
    *,
    ports: Ports,
    signing_keys: SigningKeys,
    captured: Captured,
    upload_sql: UploadSql,
    carol: SharedPrincipal,
    scanning: bool,
    reused: bool,
) -> None:
    upload_sql.reservations.append(Reservation(DOCUMENT, VERSION, 4, reused=reused))
    storage = UploadStorage(documents=cast("Any", FakeContainer()), queue=cast("Any", FakeQueue()))
    async with start(
        ports, signing_keys, settings_with(storage_malware_scanning_enabled=scanning), storage
    ) as api:
        response = await api.client.post(
            BASE, json=reservation_body(name="  Travel  Policy.PDF "), headers=carol.authorization()
        )

    assert response.status_code == 201
    assert response.json() == {
        "documentId": str(DOCUMENT),
        "versionId": str(VERSION),
        "versionNumber": 4,
        "reused": reused,
    }
    assert "Location" not in response.headers
    assert upload_sql.names() == ["upload_gate", "project_mapped", "reserve_upload"]
    assert upload_sql.call("project_mapped") == {"project_id": PROJECT, "index_id": INDEX}
    work = upload_sql.call("reserve_upload")
    assert work["name"].source_name == "Travel  Policy.PDF"
    assert work["name"].normalized_name == "travel policy.pdf"
    assert work["name"].content_type is ContentType.PDF
    assert work["sha256"] == sha(pdf_bytes(1000))
    assert (work["size_bytes"], work["project_id"], work["confirm_update"]) == (
        1000,
        PROJECT,
        False,
    )
    assert work["scan_required"] is scanning
    assert work["oid"] == carol.oid


@pytest.mark.parametrize(
    ("name", "code", "reason"),
    [
        ("bad\u202efdp.docx", "invalid_file_name", "invalid_characters"),
        ("zero\u200bwidth.pdf", "invalid_file_name", "invalid_characters"),
        ("folder/file.pdf", "invalid_file_name", "invalid_characters"),
        ("   ", "invalid_file_name", "too_short"),
        ("x" * 252 + ".pdf", "invalid_file_name", "too_long"),
        ("report.doc", "unsupported_type", "invalid_format"),
        ("report.pdf.exe", "unsupported_type", "invalid_format"),
    ],
)
async def test_t134_the_name_rules_refuse_on_reserve(
    *,
    api: ApiUnderTest,
    carol: SharedPrincipal,
    upload_sql: UploadSql,
    name: str,
    code: str,
    reason: str,
) -> None:
    response = await api.client.post(
        BASE, json=reservation_body(name=name), headers=carol.authorization()
    )

    assert response.status_code == 422
    assert response.json()["code"] == code
    assert response.json()["errors"] == [{"path": "body.name", "reason": reason}]
    assert upload_sql.names() == ["upload_gate"]


@pytest.mark.parametrize(
    ("size", "code"), [(0, "empty_file"), (MAX_BYTES + 1, "validation_failed")]
)
async def test_the_reserved_size_is_checked(
    api: ApiUnderTest, carol: SharedPrincipal, upload_sql: UploadSql, size: int, code: str
) -> None:
    response = await api.client.post(
        BASE, json=reservation_body(sizeBytes=size), headers=carol.authorization()
    )

    assert response.status_code == 422
    assert response.json()["code"] == code
    assert response.json()["errors"] == [{"path": "body.sizeBytes", "reason": "out_of_range"}]


async def test_a_project_that_doesnt_map_the_index_is_refused(
    api: ApiUnderTest, carol: SharedPrincipal, upload_sql: UploadSql
) -> None:
    upload_sql.mapped = False

    response = await api.client.post(BASE, json=reservation_body(), headers=carol.authorization())

    assert response.status_code == 422
    assert response.json()["code"] == "project_not_mapped"
    assert response.json()["errors"] == [{"path": "body.projectId", "reason": "not_allowed"}]
    assert "reserve_upload" not in upload_sql.names()


@pytest.mark.parametrize("code", ["content_unchanged", "update_not_confirmed", "duplicate_content"])
async def test_the_documents_versions_refuse_with_409(
    api: ApiUnderTest, carol: SharedPrincipal, upload_sql: UploadSql, code: Any
) -> None:
    upload_sql.reservations.append(UploadRefusedError(code))

    response = await api.client.post(BASE, json=reservation_body(), headers=carol.authorization())

    assert response.status_code == 409
    assert response.json()["code"] == code


def _name_race() -> SqlUniqueViolation:
    return SqlUniqueViolation(2627, "23000", "UQ_documents_index_name")


@pytest.mark.parametrize(
    "failures",
    [
        [_name_race(), _name_race()],
        [SqlUniqueViolation(2627, "23000", "UQ_document_versions_number")],
        [SqlDeadlock(1205, "40001")],
        [SqlUnavailable("exhausted", 1205, "40001")],
    ],
)
async def test_a_reservation_that_cant_commit_is_dependency_unavailable(
    api: ApiUnderTest, carol: SharedPrincipal, upload_sql: UploadSql, failures: list[Exception]
) -> None:
    upload_sql.reservations.extend(failures)

    response = await api.client.post(BASE, json=reservation_body(), headers=carol.authorization())

    assert response.status_code == 503
    assert response.json()["code"] == "dependency_unavailable"
    assert not upload_sql.reservations


async def test_the_reservation_body_is_validated_first(
    api: ApiUnderTest, carol: SharedPrincipal, upload_sql: UploadSql
) -> None:
    body = reservation_body(sha256="xyz", projectId="{" + str(PROJECT) + "}", extra=1)

    response = await api.client.post(BASE, json=body, headers=carol.authorization())

    assert response.status_code == 422
    assert response.json()["errors"] == [
        {"path": "body.sha256", "reason": "invalid_format"},
        {"path": "body.projectId", "reason": "invalid_uuid"},
        {"path": "body.extra", "reason": "unknown_field"},
    ]
    assert upload_sql.calls == []


# --- adminUploadContent -----------------------------------------------------------------------


async def test_t129_t134_a_stream_is_staged_in_blocks_and_committed_once_in_cool(
    api: ApiUnderTest,
    carol: SharedPrincipal,
    upload_sql: UploadSql,
    container: FakeContainer,
    captured: Captured,
) -> None:
    data = pdf_bytes(2 * BLOCK_SIZE + 1000)
    upload_sql.version = version_row(data)

    response = await put_content(api, carol, data)

    assert response.status_code == 200
    body = response.json()["version"]
    assert body["state"] == "pending"
    assert body["versionId"] == str(VERSION)
    assert body["sha256"] == sha(data).hex()
    assert body["uploadedViaProject"] == {"projectId": str(PROJECT), "name": "Travel"}
    assert body["reservedAt"] == "2026-10-31T09:00:00.000Z"
    path = f"{INDEX}/{DOCUMENT}/3/source"
    sizes = [size for _, _, size in container.staged]
    assert sizes == [BLOCK_SIZE, BLOCK_SIZE, 1000]
    ids = [identifier for _, identifier, _ in container.staged]
    nonce = ids[0].split("-")[0]
    assert len(nonce) == 32
    assert ids == [f"{nonce}-{index:08d}" for index in range(3)]
    assert all(len(identifier) == 41 for identifier in ids)
    ((committed_path, block_ids, options),) = container.commits
    assert (committed_path, block_ids) == (path, ids)
    assert options["standard_blob_tier"] is StandardBlobTier.COOL
    assert options["match_condition"] is MatchConditions.IfMissing
    assert options["content_settings"].content_type == "application/pdf"
    work = upload_sql.call("commit_upload")
    assert work["last_modified"] == LAST_MODIFIED
    assert work["actor"] == carol.oid
    (server,) = captured.server_spans()
    assert (server.attributes or {})["docchat.audit.action"] == "document.upload_committed"


async def test_t134_the_stored_type_comes_from_the_reservation_never_the_header(
    api: ApiUnderTest, carol: SharedPrincipal, upload_sql: UploadSql, container: FakeContainer
) -> None:
    data = b"PK\x03\x04" + b"\x00" * 996
    upload_sql.version = version_row(data, content_type=ContentType.DOCX.value)

    response = await put_content(api, carol, data)

    assert response.status_code == 200
    (_, _, options) = container.commits[0]
    assert options["content_settings"].content_type == ContentType.DOCX.value


@pytest.mark.parametrize(
    ("content_type", "data"),
    [
        (ContentType.PDF, b"%PDF" + b" " * 2000),
        (ContentType.PDF, b" " * 1020 + b"%PDF-1.7" + b"0" * 1000),
        (ContentType.DOCX, b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 992),
        (ContentType.PPTX, b"%PDF-1.7" + b"\x00" * 992),
        (ContentType.DOCX, b" PK\x03\x04" + b"\x00" * 995),
    ],
)
async def test_t133_t134_a_wrong_signature_stages_nothing(
    *,
    api: ApiUnderTest,
    carol: SharedPrincipal,
    upload_sql: UploadSql,
    container: FakeContainer,
    content_type: ContentType,
    data: bytes,
) -> None:
    upload_sql.version = version_row(data, content_type=content_type.value)

    response = await put_content(api, carol, data)

    assert response.status_code == 422
    assert response.json()["code"] == "file_signature_mismatch"
    assert response.json()["errors"] == [{"path": "body", "reason": "invalid_format"}]
    assert container.staged == []
    assert container.commits == []
    assert "commit_upload" not in upload_sql.names()


def test_the_pdf_signature_may_sit_anywhere_in_the_first_1024_bytes() -> None:
    assert signature_matches(b" " * 1019 + b"%PDF-", ContentType.PDF)
    assert not signature_matches(b" " * 1020 + b"%PDF-", ContentType.PDF)
    assert signature_matches(b"PK\x03\x04rest", ContentType.PPTX)
    assert not signature_matches(b"PK\x05\x06", ContentType.DOCX)


async def test_t133_a_hash_mismatch_commits_nothing(
    api: ApiUnderTest, carol: SharedPrincipal, upload_sql: UploadSql, container: FakeContainer
) -> None:
    data = pdf_bytes(5000)
    upload_sql.version = version_row(data, sha256=sha(b"other"))

    response = await put_content(api, carol, data)

    assert response.status_code == 422
    assert response.json()["code"] == "hash_mismatch"
    assert response.json()["errors"] == [{"path": "body", "reason": "invalid_format"}]
    assert len(container.staged) == 1
    assert container.commits == []


async def _pieces(*pieces: bytes) -> AsyncIterator[bytes]:
    for piece in pieces:
        yield piece


@pytest.mark.parametrize("delivered", [999, 1001])
async def test_t133_a_body_shorter_or_longer_than_declared_commits_nothing(
    api: ApiUnderTest,
    carol: SharedPrincipal,
    upload_sql: UploadSql,
    container: FakeContainer,
    delivered: int,
) -> None:
    upload_sql.version = version_row(pdf_bytes(1000))

    response = await put_content(
        api, carol, _pieces(pdf_bytes(delivered)), headers={"Content-Length": "1000"}
    )

    assert response.status_code == 422
    assert response.json()["code"] == "size_mismatch"
    assert response.json()["errors"] == [{"path": "body", "reason": "out_of_range"}]
    assert container.commits == []


@pytest.mark.parametrize(
    ("headers", "status", "code"),
    [
        ({}, 411, "content_length_required"),
        ({"Content-Length": str(MAX_BYTES + 1)}, 413, "file_too_large"),
        ({"Content-Length": "999"}, 422, "size_mismatch"),
    ],
)
async def test_the_declared_size_is_checked_before_the_first_byte(
    *,
    api: ApiUnderTest,
    carol: SharedPrincipal,
    upload_sql: UploadSql,
    container: FakeContainer,
    headers: dict[str, str],
    status: int,
    code: str,
) -> None:
    upload_sql.version = version_row(pdf_bytes(1000))

    response = await put_content(api, carol, _pieces(pdf_bytes(1000)), headers=headers)

    assert response.status_code == status
    assert response.json()["code"] == code
    if code == "size_mismatch":
        assert response.json()["errors"] == [
            {"path": "header.Content-Length", "reason": "out_of_range"}
        ]
    assert container.staged == []
    assert slots_of(api.app).in_use == 0


async def test_a_version_of_another_index_is_not_found(
    api: ApiUnderTest, carol: SharedPrincipal, upload_sql: UploadSql
) -> None:
    upload_sql.version = None

    response = await put_content(api, carol, pdf_bytes(1000))

    assert response.status_code == 404
    assert response.json()["code"] == "version_not_found"
    assert upload_sql.call("upload_version") == {"index_id": INDEX, "version_id": VERSION}


async def test_a_pending_version_answers_200_without_reading_the_body(
    api: ApiUnderTest, carol: SharedPrincipal, upload_sql: UploadSql, container: FakeContainer
) -> None:
    upload_sql.version = version_row(state="pending", committed_at=READ_AT, scan_status="awaiting")

    response = await api.client.put(
        CONTENT_URL,
        content=b"{}",
        headers=carol.authorization() | {"Content-Type": "application/json"},
    )

    assert response.status_code == 200
    version = response.json()["version"]
    assert (version["state"], version["scanStatus"]) == ("pending", "awaiting")
    assert version["committedAt"] == "2026-10-31T09:00:00.000Z"
    assert container.staged == []
    assert "commit_upload" not in upload_sql.names()


@pytest.mark.parametrize("state", ["failed", "superseded", "ingested", "ingesting"])
async def test_a_version_past_its_upload_is_not_uploading(
    api: ApiUnderTest, carol: SharedPrincipal, upload_sql: UploadSql, state: str
) -> None:
    upload_sql.version = version_row(state=state, committed_at=READ_AT)

    response = await put_content(api, carol, pdf_bytes(1000))

    assert response.status_code == 409
    assert response.json()["code"] == "version_not_uploading"


async def test_another_media_type_is_refused_at_the_first_read(
    api: ApiUnderTest, carol: SharedPrincipal, upload_sql: UploadSql, container: FakeContainer
) -> None:
    upload_sql.version = version_row(pdf_bytes(1000))

    response = await api.client.put(
        CONTENT_URL,
        content=pdf_bytes(1000),
        headers=carol.authorization() | {"Content-Type": "application/pdf"},
    )

    assert response.status_code == 415
    assert response.json()["code"] == "unsupported_media_type"
    assert container.staged == []
    assert slots_of(api.app).in_use == 0


@pytest.mark.parametrize(
    "error", [ServiceRequestError("unreachable"), HttpResponseError(message="503")]
)
async def test_a_staging_failure_is_storage_unavailable(
    api: ApiUnderTest,
    carol: SharedPrincipal,
    upload_sql: UploadSql,
    container: FakeContainer,
    error: Exception,
) -> None:
    upload_sql.version = version_row(pdf_bytes(1000))
    container.stage_error = error

    response = await put_content(api, carol, pdf_bytes(1000))

    assert response.status_code == 503
    assert response.json()["code"] == "storage_unavailable"
    assert "retryAfterSeconds" not in response.json()
    assert container.commits == []


async def test_t138_a_commit_that_meets_condition_not_met_continues_with_the_blob(
    api: ApiUnderTest, carol: SharedPrincipal, upload_sql: UploadSql, container: FakeContainer
) -> None:
    upload_sql.version = version_row(pdf_bytes(1000))
    container.commit_error = condition_not_met()

    response = await put_content(api, carol, pdf_bytes(1000))

    assert response.status_code == 200
    assert container.property_reads == [f"{INDEX}/{DOCUMENT}/3/source"]
    assert upload_sql.call("commit_upload")["last_modified"] == EARLIER


def blob_already_exists(code: str = "BlobAlreadyExists") -> ResourceExistsError:
    error = ResourceExistsError(message="exists")
    error.error_code = code  # type: ignore[attr-defined]
    return error


async def test_a_commit_that_finds_the_blob_already_there_continues_with_it(
    api: ApiUnderTest, carol: SharedPrincipal, upload_sql: UploadSql, container: FakeContainer
) -> None:
    # The storage service and Azurite answer `If-None-Match: *` on a written path with
    # `409 BlobAlreadyExists` (80-data-model.md acceptance criterion 28, T-097).
    upload_sql.version = version_row(pdf_bytes(1000))
    container.commit_error = blob_already_exists()

    response = await put_content(api, carol, pdf_bytes(1000))

    assert response.status_code == 200
    assert container.property_reads == [f"{INDEX}/{DOCUMENT}/3/source"]
    assert upload_sql.call("commit_upload")["last_modified"] == EARLIER


@pytest.mark.parametrize(
    ("commit_error", "properties_error"),
    [
        (condition_not_met("BlobOverwritten"), None),
        (blob_already_exists("LeaseIdMissing"), None),
        (HttpResponseError(message="500"), None),
        (condition_not_met(), ServiceRequestError("unreachable")),
    ],
)
async def test_a_failed_commit_is_storage_unavailable(
    *,
    api: ApiUnderTest,
    carol: SharedPrincipal,
    upload_sql: UploadSql,
    container: FakeContainer,
    commit_error: Exception,
    properties_error: Exception | None,
) -> None:
    upload_sql.version = version_row(pdf_bytes(1000))
    container.commit_error = commit_error
    container.properties_error = properties_error

    response = await put_content(api, carol, pdf_bytes(1000))

    assert response.status_code == 503
    assert response.json()["code"] == "storage_unavailable"
    assert "commit_upload" not in upload_sql.names()


@pytest.mark.parametrize("code", ["content_unchanged", "duplicate_content"])
@pytest.mark.parametrize(
    "delete_error", [None, ResourceNotFoundError("absent"), ServiceRequestError("down")]
)
async def test_a_sha256_index_refusal_deletes_the_blob_and_fails_the_version(
    *,
    api: ApiUnderTest,
    carol: SharedPrincipal,
    upload_sql: UploadSql,
    container: FakeContainer,
    code: Any,
    delete_error: Exception | None,
) -> None:
    upload_sql.version = version_row(pdf_bytes(1000))
    upload_sql.commits.append(CommitResult("duplicate", code=code))
    container.delete_error = delete_error

    response = await put_content(api, carol, pdf_bytes(1000))

    assert response.status_code == 409
    assert response.json()["code"] == code
    assert container.deleted == [f"{INDEX}/{DOCUMENT}/3/source"]
    assert upload_sql.call("fail_upload") == {"version_id": VERSION, "code": code}


async def test_a_failed_mark_after_a_refusal_still_answers_409(
    api: ApiUnderTest, carol: SharedPrincipal, upload_sql: UploadSql
) -> None:
    upload_sql.version = version_row(pdf_bytes(1000))
    upload_sql.commits.append(CommitResult("duplicate", code="duplicate_content"))
    upload_sql.fail_marks.append(SqlUnavailable("exhausted", 40613, ""))

    response = await put_content(api, carol, pdf_bytes(1000))

    assert response.status_code == 409
    assert response.json()["code"] == "duplicate_content"


async def test_a_version_failed_by_a_delete_meanwhile_is_not_uploading(
    api: ApiUnderTest, carol: SharedPrincipal, upload_sql: UploadSql
) -> None:
    upload_sql.version = version_row(pdf_bytes(1000))
    upload_sql.commits.append(CommitResult("not_uploading"))

    response = await put_content(api, carol, pdf_bytes(1000))

    assert response.status_code == 409
    assert response.json()["code"] == "version_not_uploading"


async def test_a_commit_an_earlier_attempt_made_answers_200(
    api: ApiUnderTest, carol: SharedPrincipal, upload_sql: UploadSql, captured: Captured
) -> None:
    upload_sql.version = version_row(pdf_bytes(1000))
    upload_sql.commits.append(
        CommitResult("already_pending", pending(_upload_version(upload_sql.version)))
    )

    response = await put_content(api, carol, pdf_bytes(1000))

    assert response.status_code == 200
    (server,) = captured.server_spans()
    assert "docchat.audit.action" not in (server.attributes or {})


async def test_the_upload_span_and_metrics(
    api: ApiUnderTest, carol: SharedPrincipal, upload_sql: UploadSql, captured: Captured
) -> None:
    data = pdf_bytes(3000)
    upload_sql.version = version_row(data)

    ok = await put_content(api, carol, data)
    upload_sql.version = version_row(data, sha256=sha(b"other"))
    failed = await put_content(api, carol, data)

    assert (ok.status_code, failed.status_code) == (200, 422)
    first, second = captured.finished_spans("docchat.ingestion.upload")
    assert dict(first.attributes or {}) == {
        "docchat.index_id": str(INDEX),
        "docchat.document_id": str(DOCUMENT),
        "docchat.version_id": str(VERSION),
        "docchat.outcome": "ok",
        "docchat.ingestion.upload_bytes": 3000,
    }
    assert (second.attributes or {})["docchat.outcome"] == "failed"
    assert (second.attributes or {})["docchat.error_code"] == "hash_mismatch"
    assert captured.points("docchat.uploads.completed") == {
        (("error_code", "none"), ("outcome", "ok")): 1,
        (("error_code", "hash_mismatch"), ("outcome", "failed")): 1,
    }
    sizes = histogram_points(captured, "docchat.uploads.size")
    assert sizes == {(("content_type", "application/pdf"),): (1, 3000)}


# --- Stream slots (T-136) ---------------------------------------------------------------------


async def test_t136_the_fifth_concurrent_stream_is_busy_and_every_ending_releases(
    api: ApiUnderTest, carol: SharedPrincipal, upload_sql: UploadSql
) -> None:
    data = pdf_bytes(2000)
    upload_sql.version = version_row(data)
    gate = asyncio.Event()

    async def held() -> AsyncIterator[bytes]:
        yield data[:1000]
        await gate.wait()
        yield data[1000:]

    headers = {"Content-Length": "2000"}
    streams = [
        asyncio.create_task(put_content(api, carol, held(), headers=headers)) for _ in range(4)
    ]
    async with asyncio.timeout(5):
        # The route signals nothing when it takes a slot, so the test polls for the four.
        while getattr(api.app.state, SLOTS_STATE, None) is None or slots_of(api.app).in_use < 4:  # noqa: ASYNC110
            await asyncio.sleep(0.01)

    busy = await put_content(api, carol, data)

    assert busy.status_code == 429
    assert busy.json()["code"] == "upload_busy"
    assert busy.json()["retryAfterSeconds"] == 5
    assert busy.headers["Retry-After"] == "5"
    gate.set()
    finished = await asyncio.gather(*streams)
    assert [response.status_code for response in finished] == [200] * 4
    assert slots_of(api.app).in_use == 0

    upload_sql.version = version_row(data, sha256=sha(b"other"))
    assert (await put_content(api, carol, data)).status_code == 422
    assert slots_of(api.app).in_use == 0


async def test_t136_a_client_disconnect_releases_its_slot(upload_sql: UploadSql) -> None:
    slots = UploadSlots(1)
    version = _upload_version(version_row(pdf_bytes(1000)))

    async def disconnected() -> AsyncIterator[bytes]:
        yield b"%PDF-"
        raise ClientDisconnect

    request = SimpleNamespace(headers={"content-length": "1000"}, stream=disconnected)
    process = SimpleNamespace(settings=api_settings(), sql=None)
    storage = UploadStorage(documents=cast("Any", FakeContainer()), queue=cast("Any", FakeQueue()))
    with pytest.raises(ClientDisconnect):
        await admin_uploads._upload(
            cast("Any", request),
            version,
            process=cast("Any", process),
            storage=storage,
            slots=slots,
            actor=UPLOADER,
            report=admin_uploads.UploadReport(),
        )
    assert slots.in_use == 0


def test_the_slots_are_taken_without_waiting() -> None:
    slots = UploadSlots(2)
    assert [slots.take(), slots.take(), slots.take()] == [True, True, False]
    slots.release()
    assert slots.take()
    with pytest.raises(ValueError, match="at least one"):
        UploadSlots(0)
    empty = UploadSlots(1)
    with pytest.raises(RuntimeError, match="no upload stream slot"):
        empty.release()


async def test_the_slot_count_comes_from_the_setting(
    ports: Ports,
    signing_keys: SigningKeys,
    captured: Captured,
    upload_sql: UploadSql,
    carol: SharedPrincipal,
) -> None:
    storage = UploadStorage(documents=cast("Any", FakeContainer()), queue=cast("Any", FakeQueue()))
    upload_sql.version = version_row(pdf_bytes(1000))
    async with start(
        ports, signing_keys, settings_with(upload_concurrent_streams_per_worker=7), storage
    ) as api:
        await put_content(api, carol, pdf_bytes(1000))
        assert slots_of(api.app).size == 7


# --- One 50 MB stream (T-133) -----------------------------------------------------------------

LARGE_SIZE: Final = 50 * MIB
PIECE: Final = 65_536


@dataclass(frozen=True)
class LargeFile:
    first: bytes
    rest: bytes
    digest: bytes

    async def pieces(self) -> AsyncIterator[bytes]:
        yield self.first
        for _ in range(LARGE_SIZE // PIECE - 1):
            yield self.rest


@pytest.fixture
def large_file() -> LargeFile:
    """A 50 MiB PDF body as one repeated 64 KiB piece, hashed here, outside the event loop."""
    first = b"%PDF-1.7\n" + b"a" * (PIECE - 9)
    rest = b"b" * PIECE
    hasher = hashlib.sha256(first)
    for _ in range(LARGE_SIZE // PIECE - 1):
        hasher.update(rest)
    return LargeFile(first, rest, hasher.digest())


async def test_t133_one_50_mb_stream_holds_less_than_10_mib(
    api: ApiUnderTest,
    carol: SharedPrincipal,
    upload_sql: UploadSql,
    container: FakeContainer,
    large_file: LargeFile,
) -> None:
    upload_sql.version = version_row(size_bytes=LARGE_SIZE, sha256=large_file.digest)
    headers = {**OCTET, **carol.authorization(), "Content-Length": str(LARGE_SIZE)}
    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        response = await api.client.put(CONTENT_URL, content=large_file.pieces(), headers=headers)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert response.status_code == 200
    assert len(container.staged) == LARGE_SIZE // BLOCK_SIZE + 1
    assert peak < 10 * MIB


# --- The access tier on the wire (T-129, API half) --------------------------------------------


class RecordingTransport(ReplayTransport):
    """The replay transport, keeping every request's method, URL and headers."""

    def __init__(self, exchanges: list[Exchange]) -> None:
        super().__init__(exchanges)
        self.sent: list[tuple[str, str, dict[str, str]]] = []

    @override
    async def send(self, request: HttpRequest, **kwargs: Any) -> ReplayResponse:
        self.sent.append((request.method, request.url, dict(request.headers)))
        return await super().send(request, **kwargs)


BLOB_PATH: Final = f"/documents/{INDEX}/{DOCUMENT}/3/source"


@pytest.fixture
def recording() -> RecordingTransport:
    created = {
        "status": 201,
        "headers": {
            "ETag": '"0x8DCF0"',
            "Last-Modified": "Sat, 31 Oct 2026 09:15:02 GMT",
            "x-ms-request-server-encrypted": "true",
        },
    }
    return RecordingTransport(
        [
            Exchange.from_json(
                {
                    "request": {"method": "PUT", "path": BLOB_PATH, "query": {"comp": "block"}},
                    "response": created,
                }
            ),
            Exchange.from_json(
                {
                    "request": {"method": "PUT", "path": BLOB_PATH, "query": {"comp": "blocklist"}},
                    "response": created,
                }
            ),
        ]
    )


@pytest.fixture
def documents_client(recording: RecordingTransport) -> ContainerClient:
    """A real `documents` container client over the recording transport, built outside the
    event loop."""
    return ContainerClient(
        "https://stdocchatdatadev.blob.core.windows.net", "documents", transport=recording
    )


async def test_t129_the_commit_carries_the_cool_tier_and_if_none_match(
    *,
    ports: Ports,
    signing_keys: SigningKeys,
    captured: Captured,
    upload_sql: UploadSql,
    carol: SharedPrincipal,
    recording: RecordingTransport,
    documents_client: ContainerClient,
) -> None:
    data = pdf_bytes(1000)
    upload_sql.version = version_row(data)
    storage = UploadStorage(documents=cast("Any", documents_client), queue=cast("Any", FakeQueue()))
    async with documents_client, start(ports, signing_keys, api_settings(), storage) as api:
        response = await put_content(api, carol, data)

    assert response.status_code == 200
    (stage_method, stage_url, _), (commit_method, commit_url, commit_headers) = recording.sent
    assert (stage_method, commit_method) == ("PUT", "PUT")
    assert "comp=block&" in stage_url or stage_url.endswith("comp=block")
    staged_id = stage_url.split("blockid=")[1].split("&")[0]
    nonce = base64.b64decode(staged_id.replace("%3D", "=")).decode("ascii").split("-")[0]
    assert staged_id.replace("%3D", "=") == wire_block_id(nonce, 0)
    assert "comp=blocklist" in commit_url
    headers = {name.lower(): value for name, value in commit_headers.items()}
    assert headers["x-ms-access-tier"] == "Cool"
    assert headers["if-none-match"] == "*"
    assert headers["x-ms-blob-content-type"] == "application/pdf"
    assert upload_sql.call("commit_upload")["last_modified"] == LAST_MODIFIED


# --- adminSubmitUploads -----------------------------------------------------------------------


def run_result(run_id: UUID | None, *outcomes: tuple[UUID, SubmitOutcome]) -> UploadRunResult:
    return UploadRunResult(
        run_id, tuple(SubmitResult(version, outcome) for version, outcome in outcomes)
    )


async def test_a_submit_queues_a_run_and_sends_its_message(
    api: ApiUnderTest,
    carol: SharedPrincipal,
    upload_sql: UploadSql,
    queue: FakeQueue,
    captured: Captured,
) -> None:
    run_id, other = uuid4(), uuid4()
    result = run_result(run_id, (VERSION, SubmitOutcome.QUEUED), (other, SubmitOutcome.NOT_FOUND))
    upload_sql.submitted.append(
        SubmittedUploads(result, (run_submitted_event(INDEX, result, carol.oid),))
    )

    response = await api.client.post(
        f"{BASE}/submit",
        json={"versionIds": [str(VERSION).upper(), str(other)]},
        headers=carol.authorization(),
    )

    assert response.status_code == 202
    assert response.json() == {
        "runId": str(run_id),
        "results": [
            {"versionId": str(VERSION), "outcome": "queued"},
            {"versionId": str(other), "outcome": "not_found"},
        ],
    }
    work = upload_sql.call("submit_uploads")
    assert (work["index_id"], work["version_ids"], work["actor"]) == (
        INDEX,
        [VERSION, other],
        carol.oid,
    )
    trace = work["trace"]
    assert isinstance(trace, TraceContext)
    ((content, ttl),) = queue.sent
    assert ttl == 86_400
    assert json.loads(content) == {"runId": str(run_id), "traceparent": trace.traceparent}
    (span,) = captured.finished_spans("docchat.ingestion.submit")
    assert dict(span.attributes or {}) == {
        "docchat.index_id": str(INDEX),
        "docchat.ingestion.run_kind": "upload",
        "docchat.ingestion.item_count": 1,
        "docchat.run_id": str(run_id),
    }
    assert trace.traceparent.split("-")[2] == format(span.context.span_id, "016x")
    (server,) = captured.server_spans()
    assert (server.attributes or {})["docchat.audit.action"] == "ingestion.run_submitted"


async def test_nothing_to_queue_answers_200_without_a_run(
    api: ApiUnderTest, carol: SharedPrincipal, upload_sql: UploadSql, queue: FakeQueue
) -> None:
    upload_sql.submitted.append(
        SubmittedUploads(run_result(None, (VERSION, SubmitOutcome.ALREADY_QUEUED)))
    )

    response = await api.client.post(
        f"{BASE}/submit", json={"versionIds": [str(VERSION)]}, headers=carol.authorization()
    )

    assert response.status_code == 200
    assert response.json() == {
        "runId": None,
        "results": [{"versionId": str(VERSION), "outcome": "already_queued"}],
    }
    assert queue.sent == []


async def test_a_failed_send_still_answers_202(
    api: ApiUnderTest,
    carol: SharedPrincipal,
    upload_sql: UploadSql,
    queue: FakeQueue,
    captured: Captured,
) -> None:
    run_id = uuid4()
    result = run_result(run_id, (VERSION, SubmitOutcome.QUEUED))
    upload_sql.submitted.append(SubmittedUploads(result))
    queue.error = ServiceRequestError("unreachable")

    response = await api.client.post(
        f"{BASE}/submit", json={"versionIds": [str(VERSION)]}, headers=carol.authorization()
    )

    assert response.status_code == 202
    assert captured.events("ingestion_queue_send_failed") == [{"runId": str(run_id)}]


async def test_a_repeated_version_is_refused_before_the_gate(
    api: ApiUnderTest, carol: SharedPrincipal, upload_sql: UploadSql
) -> None:
    other = uuid4()
    ids = [str(VERSION), str(other), str(VERSION).upper(), str(other)]

    response = await api.client.post(
        f"{BASE}/submit", json={"versionIds": ids}, headers=carol.authorization()
    )

    assert response.status_code == 422
    assert response.json()["code"] == "validation_failed"
    assert response.json()["errors"] == [
        {"path": "body.versionIds.2", "reason": "duplicate"},
        {"path": "body.versionIds.3", "reason": "duplicate"},
    ]
    assert upload_sql.calls == []


async def test_t137_submit_refuses_more_than_100_versions(
    api: ApiUnderTest, carol: SharedPrincipal, upload_sql: UploadSql
) -> None:
    ids = [str(uuid4()) for _ in range(101)]

    response = await api.client.post(
        f"{BASE}/submit", json={"versionIds": ids}, headers=carol.authorization()
    )

    assert response.status_code == 422
    assert response.json()["errors"] == [{"path": "body.versionIds", "reason": "too_many_items"}]
    assert upload_sql.calls == []


async def test_an_index_with_nothing_to_target_takes_no_submit(
    api: ApiUnderTest, carol: SharedPrincipal, upload_sql: UploadSql, queue: FakeQueue
) -> None:
    upload_sql.submitted.append(IngestionRefusedError("index_not_accepting_uploads"))

    response = await api.client.post(
        f"{BASE}/submit", json={"versionIds": [str(VERSION)]}, headers=carol.authorization()
    )

    assert response.status_code == 409
    assert response.json()["code"] == "index_not_accepting_uploads"
    assert queue.sent == []


# --- Helpers ----------------------------------------------------------------------------------


def test_block_ids_are_41_characters_and_fit_the_service_limit() -> None:
    nonce = "0123456789abcdef0123456789abcdef"
    assert block_id(nonce, 7) == f"{nonce}-00000007"
    assert len(block_id(nonce, 99_999_999)) == 41
    assert len(wire_block_id(nonce, 0).encode("ascii")) <= 64
    assert base64.b64decode(wire_block_id(nonce, 3)) == f"{nonce}-00000003".encode()


async def _collect(chunks: list[bytes], declared: int) -> list[int]:
    async def source() -> AsyncIterator[bytes]:
        for chunk in chunks:
            yield chunk

    return [len(block) async for block in blocks_of(source(), declared=declared)]


@pytest.mark.parametrize(
    ("chunks", "blocks"),
    [
        ([b"x" * BLOCK_SIZE], [BLOCK_SIZE]),
        ([b"x" * (BLOCK_SIZE - 1), b"", b"xx"], [BLOCK_SIZE, 1]),
        ([b"x" * (2 * BLOCK_SIZE + 3)], [BLOCK_SIZE, BLOCK_SIZE, 3]),
        ([b"x" * 10, b"y" * 20], [30]),
        ([], []),
    ],
)
async def test_the_stream_is_cut_into_4_mib_blocks(chunks: list[bytes], blocks: list[int]) -> None:
    assert await _collect(chunks, sum(map(len, chunks))) == blocks


async def test_a_block_keeps_the_bytes_in_order() -> None:
    async def source() -> AsyncIterator[bytes]:
        yield b"a" * (BLOCK_SIZE - 2)
        yield b"bcd"

    blocks = [block async for block in blocks_of(source(), declared=BLOCK_SIZE + 1)]
    assert blocks[0][-2:] == b"bc"
    assert blocks[1] == b"d"


async def test_a_count_above_the_declared_size_stops_the_stream() -> None:
    with pytest.raises(DocChatProblem) as stopped:
        await _collect([b"x" * 10, b"y"], 10)
    assert stopped.value.entry.code == "size_mismatch"


def test_the_failure_message_of_each_kind_of_code() -> None:
    assert failure_message(None) is None
    assert failure_message("document_deleted") == "Not processed because the document was deleted."
    assert failure_message("duplicate_content") == (
        "The same file is already in this index under another name."
    )
    assert failure_message("not_a_code") is None


def test_check_batch_names_the_setting() -> None:
    check_batch(5, 5, "body.files")
    with pytest.raises(DocChatProblem) as refused:
        check_batch(6, 5, "body.files")
    assert refused.value.detail == "Send at most 5 files at a time."


def test_duplicate_issues_name_each_repeat() -> None:
    first, second = uuid4(), uuid4()
    assert duplicate_issues([first, second]) == []
    assert [issue.path for issue in duplicate_issues([first, first, second, first])] == [
        "body.versionIds.1",
        "body.versionIds.3",
    ]


def test_declared_length_reads_only_digits() -> None:
    def request(value: str | None) -> Any:
        headers = {} if value is None else {"content-length": value}
        return SimpleNamespace(headers=headers)

    assert declared_length(request("1000")) == 1000
    for value in (None, "", "-1", "1e3", "١٢"):
        assert declared_length(request(value)) is None


async def test_a_message_needs_a_trace_context() -> None:
    queue = FakeQueue()
    await send_run_message(cast("Any", queue), uuid4(), None)
    assert queue.sent == []


async def test_discarding_an_absent_or_unreachable_blob_never_raises() -> None:
    for error in (None, ResourceNotFoundError("absent"), ServiceRequestError("down")):
        container = FakeContainer(delete_error=error)
        await discard_blob(cast("Any", container.get_blob_client("p")))
        assert container.deleted == ["p"]


async def test_commit_blob_returns_the_commits_last_modified() -> None:
    container = FakeContainer()
    blob = cast("Any", container.get_blob_client("p"))
    assert await commit_blob(blob, ["a", "b"], ContentType.PPTX) == LAST_MODIFIED
    ((_, ids, options),) = container.commits
    assert ids == ["a", "b"]
    assert isinstance(options["content_settings"], ContentSettings)
    assert options["content_settings"].content_type == ContentType.PPTX.value


def test_upload_storage_comes_from_the_client_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []

    class Registry:
        def blob_container(self, name: str) -> object:
            calls.append(("blob", name))
            return "documents-client"

        def queue(self, name: str) -> object:
            calls.append(("queue", name))
            return "queue-client"

    monkeypatch.setattr(ClientRegistry, "current", classmethod(lambda cls: Registry()))
    storage = upload_storage()
    assert (cast("object", storage.documents), cast("object", storage.queue)) == (
        "documents-client",
        "queue-client",
    )
    assert calls == [("blob", "documents"), ("queue", "ingestion-requests")]
